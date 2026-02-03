"""
Multithreading utilities for concurrent Redis operations.

This module provides lightweight threading abstractions that enable Popoto to
perform non-blocking Redis operations. While Redis itself is single-threaded
and operations are atomic, client applications often benefit from concurrent
I/O when performing bulk operations or when Redis operations shouldn't block
the main application thread.

Design Philosophy:
    These utilities favor simplicity over sophistication. Rather than introducing
    complex async patterns or heavy concurrency frameworks, they provide minimal
    wrappers around Python's threading primitives. This keeps the codebase
    accessible while still enabling significant performance gains for I/O-bound
    Redis workloads.

Trade-offs:
    - Uses daemon threads by default, meaning background work may be abandoned
      if the main program exits. This is intentional for fire-and-forget patterns
      like pub/sub publishing or cache warming.
    - The thread pool uses a dynamic size based on input and CPU count,
      which works well for I/O-bound Redis operations.
    - No built-in retry logic or error handling - callers are responsible for
      resilience patterns.

Integration with Popoto:
    These utilities complement Popoto's synchronous API by enabling patterns like:
    - Background model saves that don't block request/response cycles
    - Parallel bulk queries across multiple key patterns
    - Concurrent pub/sub message publishing

See Also:
    - Python threading documentation: https://docs.python.org/3/library/threading.html
    - multiprocessing.dummy for thread-based parallelism with Pool interface
"""

import os
from collections.abc import Callable
from threading import Thread


def start_new_thread(function) -> Callable:
    """
    Decorator that runs a function in a background daemon thread.

    This decorator transforms a synchronous function into one that returns
    immediately while the actual work continues in the background. It's
    particularly useful for Redis operations where you don't need to wait
    for confirmation, such as publishing events or updating caches.

    The decorated function will:
        - Start immediately in a new thread
        - Run as a daemon (won't prevent program exit)
        - Not return any value to the caller (fire-and-forget pattern)

    Example:
        @start_new_thread
        def publish_event(channel, message):
            redis_client.publish(channel, message)

        # Returns immediately, publish happens in background
        publish_event("updates", "model_saved")

    Warning:
        When using this with Django's ORM alongside Popoto, you must close
        the database connection at the end of your function to prevent
        connection leaks::

            from django.db import connection
            connection.close()

        This is not needed for pure Popoto/Redis operations since Redis
        connections are managed differently.

    Args:
        function: The function to wrap for background execution.

    Returns:
        A wrapper function that spawns the original in a daemon thread.
    """

    def decorator(*args, **kwargs):
        t = Thread(target=function, args=args, kwargs=kwargs)
        t.daemon = True
        t.start()

    return decorator


def run_all_multithreaded(function_def, list_of_params):
    """
    Execute a function across multiple inputs using a thread pool.

    This function provides a simple way to parallelize Redis operations when
    you need to process multiple items. It uses a thread pool to limit
    concurrency, preventing resource exhaustion while still achieving
    parallelism.

    The thread pool approach is well-suited for Popoto workloads because:
        - Redis operations are I/O-bound, not CPU-bound
        - Network latency dominates, so parallelism hides wait time
        - Redis can handle many concurrent connections efficiently

    Example:
        # Fetch multiple models in parallel
        def fetch_user(user_id):
            return User.query.get(id=user_id)

        users = run_all_multithreaded(fetch_user, [1, 2, 3, 4, 5])

        # Process with multiple parameters using tuples
        def save_with_ttl(args):
            model, ttl = args
            model.save(expire=ttl)
            return model

        results = run_all_multithreaded(save_with_ttl, [
            (model1, 3600),
            (model2, 7200),
        ])

    Design Notes:
        - Uses dynamic thread pool sizing based on input size and CPU count
        - Blocks until all operations complete (unlike start_new_thread)
        - Preserves result ordering matching input parameter order
        - Uses multiprocessing.dummy which provides Pool interface over threads

    Args:
        function_def: The function to execute for each parameter set.
        list_of_params: Iterable of parameters to pass to the function.
            For single-argument functions, pass values directly.
            For multi-argument functions, pass tuples and unpack in function.

    Returns:
        List of results from each function call, in the same order as inputs.
    """
    from multiprocessing.dummy import Pool as ThreadPool

    pool = ThreadPool(min(len(list_of_params), os.cpu_count() or 4))

    results = pool.map(function_def, list_of_params)

    pool.close()
    pool.join()

    return results
