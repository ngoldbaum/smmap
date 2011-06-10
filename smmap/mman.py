"""Module containnig a memory memory manager which provides a sliding window on a number of memory mapped files"""
from util import (
					MapWindow,
					MapRegion,
					MapRegionList,
					is_64_bit,
					align_to_mmap
				)

from weakref import ref
import sys
from sys import getrefcount

__all__ = ["StaticWindowMapManager", "SlidingWindowMapManager"]
#{ Utilities

#}END utilities



class WindowCursor(object):
	"""Pointer into the mapped region of the memory manager, keeping the map 
	alive until it is destroyed and no other client uses it.
	
	Cursors should not be created manually, but are instead returned by the SlidingWindowMapManager
	:note: The current implementation is suited for static and sliding window managers, but it also means 
		that it must be suited for the somewhat quite different sliding manager. It could be improved, but 
		I see no real need to do so."""
	__slots__ = ( 
					'_manager',	# the manger keeping all file regions
					'_rlist',	# a regions list with regions for our file
					'_region',	# our current region or None
					'_ofs',		# relative offset from the actually mapped area to our start area
					'_size'		# maximum size we should provide
				)
	
	def __init__(self, manager = None, regions = None):
		self._manager = manager
		self._rlist = regions
		self._region = None
		self._ofs = 0
		self._size = 0
		
	def __del__(self):
		self._destroy()
		
	def _destroy(self):
		"""Destruction code to decrement counters"""
		self.unuse_region()
		
		if self._rlist is not None:
			# Actual client count, which doesn't include the reference kept by the manager, nor ours
			# as we are about to be deleted
			num_clients = self._rlist.client_count() - 2
			if num_clients == 0 and len(self._rlist) == 0:
				# Free all resources associated with the mapped file
				self._manager._fdict.pop(self._rlist.path_or_fd())
			#END remove regions list from manager
		#END handle regions
		
	def _copy_from(self, rhs):
		"""Copy all data from rhs into this instance, handles usage count"""
		self._manager = rhs._manager
		self._rlist = rhs._rlist
		self._region = rhs._region
		self._ofs = rhs._ofs
		self._size = rhs._size
		
		if self._region is not None:
			self._region.increment_usage_count(1)
		# END handle regions
		
	def __copy__(self):
		"""copy module interface"""
		cpy = type(self)()
		cpy._copy_from(self)
		return cpy
		
	#{ Interface
	def assign(self, rhs):
		"""Assign rhs to this instance. This is required in order to get a real copy.
		Alternativly, you can copy an existing instance using the copy module"""
		self._destroy()
		self._copy_from(rhs)
		
	def use_region(self, offset, size = 0, flags = 0):
		"""Assure we point to a window which allows access to the given offset into the file
		:param offset: absolute offset in bytes into the file
		:param size: amount of bytes to map. If 0, all available bytes will be mapped
		:param flags: additional flags to be given to os.open in case a file handle is initially opened
			for mapping. Has no effect if a region can actually be reused.
		:return: this instance - it should be queried for whether it points to a valid memory region.
			This is not the case if the mapping failed becaues we reached the end of the file
		:note: The size actually mapped may be smaller than the given size. If that is the case,
			either the file has reached its end, or the map was created between two existing regions"""
		need_region = True
		man = self._manager
		fsize = self._rlist.file_size()
		size = min(size or fsize, man.window_size() or fsize) 	# clamp size to window size
		
		if self._region is not None:
			if self._region.includes_ofs(offset):
				need_region = False
			else:
				self.unuse_region()
			# END handle existing region
		# END check existing region
		
		# offset too large ?
		if offset >= fsize:
			return self
		#END handle offset
		
		if need_region:
			self._region = man._obtain_region(self._rlist, offset, size, flags, False)
		#END need region handling
		
		self._region.increment_usage_count()
		self._ofs = offset - self._region._b
		self._size = min(size, self._region.ofs_end() - offset)
		
		return self
		
	def unuse_region(self):
		"""Unuse the ucrrent region. Does nothing if we have no current region
		:note: the cursor unuses the region automatically upon destruction. It is recommended
			to unuse the region once you are done reading from it in persistent cursors as it 
			helps to free up resource more quickly"""
		self._region = None
		# note: should reset ofs and size, but we spare that for performance. Its not 
		# allowed to query information if we are not valid !

	def buffer(self):
		"""Return a buffer object which allows access to our memory region from our offset
		to the window size. Please note that it might be smaller than you requested when calling use_region()
		:note: You can only obtain a buffer if this instance is_valid() !
		:note: buffers should not be cached passed the duration of your access as it will 
			prevent resources from being freed even though they might not be accounted for anymore !"""
		return buffer(self._region.map(), self._ofs, self._size)
		
	def map(self):
		"""
		:return: the underlying raw memory map. Please not that the offset and size is likely to be different
			to what you set as offset and size. Use it only if you are sure about the region it maps, which is the whole
			file in case of StaticWindowMapManager"""
		return self._region.map()
		
	def is_valid(self):
		""":return: True if we have a valid and usable region"""
		return self._region is not None
		
	def is_associated(self):
		""":return: True if we are associated with a specific file already"""
		return self._rlist is not None
		
	def ofs_begin(self):
		""":return: offset to the first byte pointed to by our cursor
		:note: only if is_valid() is True"""
		return self._region._b + self._ofs
		
	def ofs_end(self):
		""":return: offset to one past the last available byte"""
		# unroll method calls for performance !
		return self._region._b + self._ofs + self._size
		
	def size(self):
		""":return: amount of bytes we point to"""
		return self._size
		
	def region_ref(self):
		""":return: weak ref to our mapped region.
		:raise AssertionError: if we have no current region. This is only useful for debugging"""
		if self._region is None:
			raise AssertionError("region not set")
		return ref(self._region)
		
	def includes_ofs(self, ofs):
		""":return: True if the given absolute offset is contained in the cursors 
			current region
		:note: cursor must be valid for this to work"""
		# unroll methods
		return (self._region._b + self._ofs) <= ofs < (self._region._b + self._ofs + self._size)
		
	def file_size(self):
		""":return: size of the underlying file"""
		return self._rlist.file_size()
		
	def path_or_fd(self):
		""":return: path or file decriptor of the underlying mapped file"""
		return self._rlist.path_or_fd()

	def path(self):
		""":return: path of the underlying mapped file
		:raise ValueError: if attached path is not a path"""
		if isinstance(self._rlist.path_or_fd(), int):
			raise ValueError("Path queried although mapping was applied to a file descriptor")
		# END handle type
		return self._rlist.path_or_fd()
		
	def fd(self):
		""":return: file descriptor used to create the underlying mapping.
		:note: it is not required to be valid anymore
		:raise ValueError: if the mapping was not created by a file descriptor"""
		if isinstance(self._rlist.path_or_fd(), basestring):
			raise ValueError("File descriptor queried although mapping was generated from path")
		#END handle type
		return self._rlist.path_or_fd()
	
	#} END interface
	
	
class StaticWindowMapManager(object):
	"""Provides a manager which will produce single size cursors that are allowed
	to always map the whole file.
	
	Clients must be written to specifically know that they are accessing their data
	through a StaticWindowMapManager, as they otherwise have to deal with their window size.
	
	These clients would have to use a SlidingWindowMapBuffer to hide this fact.
	
	This type will always use a maximum window size, and optimize certain methods to 
	acomodate this fact"""
		
	__slots__ = [
					'_fdict', 			# mapping of path -> StorageHelper (of some kind
					'_window_size', 	# maximum size of a window
					'_max_memory_size',	# maximum amount ofmemory we may allocate
					'_max_handle_count',		# maximum amount of handles to keep open
					'_memory_size',		# currently allocated memory size
					'_handle_count',		# amount of currently allocated file handles
				]
				
	#{ Configuration
	MapRegionListCls = MapRegionList
	MapWindowCls = MapWindow
	MapRegionCls = MapRegion
	WindowCursorCls = WindowCursor
	#} END configuration
				
	_MB_in_bytes = 1024 * 1024
				
	def __init__(self, window_size = 0, max_memory_size = 0, max_open_handles = sys.maxint):
		"""initialize the manager with the given parameters.
		:param window_size: if -1, a default window size will be chosen depending on 
			the operating system's architechture. It will internally be quantified to a multiple of the page size
			If 0, the window may have any size, which basically results in mapping the whole file at one
		:param max_memory_size: maximum amount of memory we may map at once before releasing mapped regions.
			If 0, a viable default iwll be set dependning on the system's architecture.
			It is a soft limit that is tried to be kept, but nothing bad happens if we have to overallocate
		:param max_open_handles: if not maxin, limit the amount of open file handles to the given number.
			Otherwise the amount is only limited by the system iteself. If a system or soft limit is hit, 
			the manager will free as many handles as posisble"""
		self._fdict = dict()
		self._window_size = window_size
		self._max_memory_size = max_memory_size
		self._max_handle_count = max_open_handles
		self._memory_size = 0
		self._handle_count = 0
		
		if window_size < 0:
			coeff = 32
			if is_64_bit():
				coeff = 1024
			#END handle arch
			self._window_size = coeff * self._MB_in_bytes
		# END handle max window size
		
		if max_memory_size == 0:
			coeff = 512
			if is_64_bit():
				coeff = 8192
			#END handle arch
			self._max_memory_size = coeff * self._MB_in_bytes
		#END handle max memory size
	
	#{ Internal Methods
	
	def _collect_lru_region(self, size):
		"""Unmap the region which was least-recently used and has no client
		:param size: size of the region we want to map next (assuming its not already mapped partially or full
			if 0, we try to free any available region
		:return: Amount of freed regions
		:note: We don't raise exceptions anymore, in order to keep the system working, allowing temporary overallocation.
			If the system runs out of memory, it will tell.
		:todo: implement a case where all unusued regions are discarded efficiently. Currently its only brute force"""
		num_found = 0
		while (size == 0) or (self._memory_size + size > self._max_memory_size):
			lru_region = None
			lru_list = None
			for regions in self._fdict.itervalues():
				for region in regions:
					# check client count - consider that we keep one reference ourselves !
					if (region.client_count()-2 == 0 and 
						(lru_region is None or region._uc < lru_region._uc)):
						lru_region = region
						lru_list = regions
					# END update lru_region
				#END for each region
			#END for each regions list
			
			if lru_region is None:
				break
			#END handle region not found
			
			num_found += 1
			del(lru_list[lru_list.index(lru_region)])
			self._memory_size -= lru_region.size()
			self._handle_count -= 1
		#END while there is more memory to free
		return num_found
		
	def _obtain_region(self, a, offset, size, flags, is_recursive):
		"""Utilty to create a new region - for more information on the parameters, 
		see MapCursor.use_region.
		:param a: A regions (a)rray
		:return: The newly created region"""
		if self._memory_size + size > self._max_memory_size:
			self._collect_lru_region(size)
		#END handle collection
		
		r = None
		if a:
			assert len(a) == 1
			r = a[0]
		else:
			try:
				r = self.MapRegionCls(a.path_or_fd(), 0, sys.maxint, flags)
			except Exception:
				# apparently we are out of system resources or hit a limit
				# As many more operations are likely to fail in that condition (
				# like reading a file from disk, etc) we free up as much as possible
				# As this invalidates our insert position, we have to recurse here
				# NOTE: The c++ version uses a linked list to curcumvent this, but
				# using that in python is probably too slow anyway
				if is_recursive:
					# we already tried this, and still have no success in obtaining 
					# a mapping. This is an exception, so we propagate it
					raise
				#END handle existing recursion
				self._collect_lru_region(0)
				return self._obtain_region(a, offset, size, flags, True) 
			#END handle exceptions
			
			self._handle_count += 1
			self._memory_size += r.size()
			a.append(r)
		# END handle array
		
		assert r.includes_ofs(offset)
		#assert r.includes_ofs(offset+size-1)
		return r

	#}END internal methods
	
	#{ Interface 
	def make_cursor(self, path_or_fd):
		""":return: a cursor pointing to the given path or file descriptor. 
		It can be used to map new regions of the file into memory
		:note: if a file descriptor is given, it is assumed to be open and valid,
			but may be closed afterwards. To refer to the same file, you may reuse
			your existing file descriptor, but keep in mind that new windows can only
			be mapped as long as it stays valid. This is why the using actual file paths
			are preferred unless you plan to keep the file descriptor open.
		:note: file descriptors are problematic as they are not necessarily unique, as two 
			different files opened and closed in succession might have the same file descriptor id. 
		:note: Using file descriptors directly is faster once new windows are mapped as it 
			prevents the file to be opened again just for the purpose of mapping it."""
		regions = self._fdict.get(path_or_fd)
		if regions is None:
			regions = self.MapRegionListCls(path_or_fd)
			self._fdict[path_or_fd] = regions
		# END obtain region for path
		return self.WindowCursorCls(self, regions)
		
	def collect(self):
		"""Collect all available free-to-collect mapped regions
		:return: Amount of freed handles"""
		return self._collect_lru_region(0)
		
	def num_file_handles(self):
		""":return: amount of file handles in use. Each mapped region uses one file handle"""
		return self._handle_count
	
	def num_open_files(self):
		"""Amount of opened files in the system"""
		return reduce(lambda x,y: x+y, (1 for rlist in self._fdict.itervalues() if len(rlist) > 0), 0)
		
	def window_size(self):
		""":return: size of each window when allocating new regions"""
		return self._window_size
		
	def mapped_memory_size(self):
		""":return: amount of bytes currently mapped in total"""
		return self._memory_size
		
	def max_file_handles(self):
		""":return: maximium amount of handles we may have opened"""
		return self._max_handle_count
		
	def max_mapped_memory_size(self):
		""":return: maximum amount of memory we may allocate"""
		return self._max_memory_size
	
	#} END interface
	
	#{ Special Purpose Interface
	
	def force_map_handle_removal_win(self, base_path):
		"""ONLY AVAILABLE ON WINDOWS
		On windows removing files is not allowed if anybody still has it opened.
		If this process is ourselves, and if the whole process uses this memory
		manager (as far as the parent framework is concerned) we can enforce
		closing all memory maps whose path matches the given base path to 
		allow the respective operation after all.
		The respective system must NOT access the closed memory regions anymore !
		This really may only be used if you know that the items which keep 
		the cursors alive will not be using it anymore. They need to be recreated !
		:return: Amount of closed handles
		:note: does nothing on non-windows platforms"""
		if sys.platform != 'win32':
			return
		#END early bailout
		
		num_closed = 0
		for path, rlist in self._fdict.iteritems():
			if path.startswith(base_path):
				for region in rlist:
					region._mf.close()
					num_closed += 1
			#END path matches 
		#END for each path
		return num_closed
	#} END special purpose interface
	
	
	
class SlidingWindowMapManager(StaticWindowMapManager):
	"""Maintains a list of ranges of mapped memory regions in one or more files and allows to easily 
	obtain additional regions assuring there is no overlap.
	Once a certain memory limit is reached globally, or if there cannot be more open file handles 
	which result from each mmap call, the least recently used, and currently unused mapped regions
	are unloaded automatically.
	
	:note: currently not thread-safe !
	:note: in the current implementation, we will automatically unload windows if we either cannot
		create more memory maps (as the open file handles limit is hit) or if we have allocated more than 
		a safe amount of memory already, which would possibly cause memory allocations to fail as our address
		space is full."""
		
	__slots__ = tuple()
	
	def __init__(self, window_size = -1, max_memory_size = 0, max_open_handles = sys.maxint):
		"""Adjusts the default window size to -1"""
		super(SlidingWindowMapManager, self).__init__(window_size, max_memory_size, max_open_handles)
	
	def _obtain_region(self, a, offset, size, flags, is_recursive):
		# bisect to find an existing region. The c++ implementation cannot 
		# do that as it uses a linked list for regions.
		r = None
		lo = 0
		hi = len(a)
		while lo < hi:
			mid = (lo+hi)//2
			ofs = a[mid]._b
			if ofs <= offset:
				if a[mid].includes_ofs(offset):
					r = a[mid]
					break
				#END have region
				lo = mid+1
			else:
				hi = mid
			#END handle position
		#END while bisecting
		
		if r is None:
			window_size = self._window_size
			left = self.MapWindowCls(0, 0)
			mid = self.MapWindowCls(offset, size)
			right = self.MapWindowCls(a.file_size(), 0)
			
			# we want to honor the max memory size, and assure we have anough
			# memory available
			# Save calls !
			if self._memory_size + window_size > self._max_memory_size:
				self._collect_lru_region(window_size)
			#END handle collection
			
			# we assume the list remains sorted by offset
			insert_pos = 0
			len_regions = len(a)
			if len_regions == 1:
				if a[0]._b <= offset:
					insert_pos = 1
				#END maintain sort
			else:
				# find insert position
				insert_pos = len_regions
				for i, region in enumerate(a):
					if region._b > offset:
						insert_pos = i
						break
					#END if insert position is correct
				#END for each region
			# END obtain insert pos
			
			# adjust the actual offset and size values to create the largest 
			# possible mapping
			if insert_pos == 0:
				if len_regions:
					right = self.MapWindowCls.from_region(a[insert_pos])
				#END adjust right side 
			else:
				if insert_pos != len_regions:
					right = self.MapWindowCls.from_region(a[insert_pos])
				# END adjust right window
				left = self.MapWindowCls.from_region(a[insert_pos - 1])
			#END adjust surrounding windows
			
			mid.extend_left_to(left, window_size)
			mid.extend_right_to(right, window_size)
			mid.align()
			
			# it can happen that we align beyond the end of the file
			if mid.ofs_end() > right.ofs:
				mid.size = right.ofs - mid.ofs
			#END readjust size
			
			# insert new region at the right offset to keep the order
			try:
				if self._handle_count >= self._max_handle_count:
					raise Exception
				#END assert own imposed max file handles
				r = self.MapRegionCls(a.path_or_fd(), mid.ofs, mid.size, flags)
			except Exception:
				# apparently we are out of system resources or hit a limit
				# As many more operations are likely to fail in that condition (
				# like reading a file from disk, etc) we free up as much as possible
				# As this invalidates our insert position, we have to recurse here
				# NOTE: The c++ version uses a linked list to curcumvent this, but
				# using that in python is probably too slow anyway
				if is_recursive:
					# we already tried this, and still have no success in obtaining 
					# a mapping. This is an exception, so we propagate it
					raise
				#END handle existing recursion
				self._collect_lru_region(0)
				return self._obtain_region(a, offset, size, flags, True) 
			#END handle exceptions
			
			self._handle_count += 1
			self._memory_size += r.size()
			a.insert(insert_pos, r)
		# END create new region
		return r
		
	
