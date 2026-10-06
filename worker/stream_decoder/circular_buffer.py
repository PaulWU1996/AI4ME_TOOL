"""
Circular Buffer: Fixed-size ring buffer implementation.

A circular buffer (ring buffer) is a fixed-size buffer that wraps around,
reusing memory when the write pointer reaches the end.
"""

import logging
import threading
from typing import Optional, List, Any, Iterator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("CircularBuffer")


class CircularBuffer:
    """
    A fixed-size circular buffer that efficiently stores elements in a ring.
    
    When the buffer is full, new writes overwrite the oldest elements.
    Supports typical queue operations: enqueue, dequeue, peek, etc.
    """
    
    def __init__(self, capacity: int):
        """
        Initialize the circular buffer.
        
        Args:
            capacity: Maximum number of elements the buffer can hold
            
        Raises:
            ValueError: If capacity is less than 1
        """
        if capacity < 1:
            raise ValueError("Capacity must be at least 1")
        
        self.capacity = capacity
        self.buffer = [None] * capacity
        self.head = 0  # Points to the next write position
        self.tail = 0  # Points to the next read position
        self.size = 0  # Current number of elements in buffer

        # semaphores to track available slots and pause threads when buffer is empty/full
        self.empty_slots = threading.Semaphore(capacity)  # Tracks available slots for writing
        self.filled_slots = threading.Semaphore(0)  # Tracks available items for reading
        self.lock = threading.Lock()  # Lock for thread-safe operations
       
        logger.info(f"Initialized CircularBuffer with capacity={capacity}")


    
    def push(self, item: Any) :
        """
        Add an item to the buffer.
        
        If the buffer is full, pauses the thread until space is available. 
        
        Args:
            item: Item to add to the buffer
            
        Returns:
            True if item was added, False if buffer was full (overwrite occurred)
        """
        self.empty_slots.acquire()  # Wait until there is space to write

        with self.lock:
            self.buffer[self.head] = item
            self.head = (self.head + 1) % self.capacity
            self.size += 1     
            logger.debug(f"Enqueued item, size={self.size}")
        self.filled_slots.release()  # Signal that there is a new item available

    
    def pop(self, timeout: Optional[float] = None) -> Optional[Any]:
        """
        Remove and return the oldest item from the buffer. 
        Blocks the thread if the buffer is empty until an item is available.
        
        Args:
            timeout: Maximum time to wait in seconds. None = wait forever.
                    If timeout expires, returns None.
        
        Returns:
            The oldest item in the buffer, or None if timeout expired with no item available
        """
        # Wait until there is an item to read (with optional timeout)
        if not self.filled_slots.acquire(timeout=timeout):
            # Timeout expired - no item available within the time limit
            return None

        with self.lock:
            item = self.buffer[self.tail]
            self.buffer[self.tail] = None  # Clear the slot
            self.tail = (self.tail + 1) % self.capacity
            self.size -= 1
            logger.debug(f"Dequeued item, size={self.size}")
        self.empty_slots.release()  # Signal that there is a new empty slot available
        return item
    
    def peek(self, index: int = 0) -> Optional[Any]:
        """
        View an item without removing it.
        
        Args:
            index: Position from the front (0 = oldest item)
            
        Returns:
            Item at the specified index, or None if index is out of range
        """
        if index < 0 or index >= self.size:
            logger.debug(f"Peek index {index} out of range (size={self.size})")
            return None
        
        actual_index = (self.tail + index) % self.capacity
        return self.buffer[actual_index]
    
    def is_empty(self) -> bool:
        """Check if the buffer is empty."""
        return self.size == 0
    
    def is_full(self) -> bool:
        """Check if the buffer is full."""
        return self.size == self.capacity
    
    def get_size(self) -> int:
        """Get the current number of items in the buffer."""
        return self.size
    
    def get_capacity(self) -> int:
        """Get the maximum capacity of the buffer."""
        return self.capacity
    
    def get_available(self) -> int:
        """Get the number of available (empty) slots."""
        return self.capacity - self.size
    
    def clear(self):
        """Clear all items from the buffer."""
        self.buffer = [None] * self.capacity
        self.head = 0
        self.tail = 0
        self.size = 0
        logger.info("Cleared buffer")
    
    def get_all(self) -> List[Any]:
        """
        Get all items in the buffer as a list (in order).
        
        Returns:
            List of items from oldest to newest
        """
        result = []
        for i in range(self.size):  
            result.append(self.peek(i))
        return result
    
    def __iter__(self) -> Iterator[Any]:
        """Iterate over items in the buffer (oldest to newest)."""
        for i in range(self.size):
            yield self.peek(i)
    
    def __len__(self) -> int:
        """Return the current number of items using len()."""
        return self.size

    
    def __getitem__(self, index: int) -> Any:
        """Support indexing with [] operator."""
        item = self.peek(index)
        if item is None and index >= self.size:
            raise IndexError(f"Index {index} out of range (size={self.size})")
        return item
    
    def __str__(self) -> str:
        """String representation of the buffer."""
        items = self.get_all()
        return f"CircularBuffer({items}, size={self.size}/{self.capacity})"
    
    def __repr__(self) -> str:
        """Detailed string representation."""
        return (
            f"CircularBuffer(capacity={self.capacity}, size={self.size}, "
            f"head={self.head}, tail={self.tail})"
        )
    
    def get_stats(self) -> dict:
        """
        Get statistics about the buffer.
        
        Returns:
            Dictionary with buffer statistics
        """
        return {
            'capacity': self.capacity,
            'size': self.size,
            'available': self.get_available(),
            'utilization_percent': (self.size / self.capacity * 100) if self.capacity else 0,
            'is_empty': self.is_empty(),
            'is_full': self.is_full(),
            'head': self.head,
            'tail': self.tail
        }

