#! /usr/bin/env python 
from twisted.internet.defer import Deferred, DeferredQueue, inlineCallbacks, returnValue
from twisted.internet import reactor
from collections import deque
from weakcallback import WeakCallbackCb
import weakref
import sys

def async_sleep(t):
    d = Deferred()
    reactor.callLater(t, d.callback, None)
    return d

def event_queue(event):
    q = DeferredQueue()
    def cb(*args, **kwargs):
        q.put((args, kwargs))
    h = event.subscribe_repeating(cb)
    q.unsubscribe = h.unsubscribe
    return q

class EventStream:
    """Event stream class to be used with select and switch."""
    def __init__(self):
        self._queue = deque()
        self._invoke_listener = None
        self.put = WeakCallbackCb(self._put)

    def _put(*args, **kwargs):
        self = args[0]
        self._queue.append((args[1:], kwargs))
        if self._invoke_listener:
            self._trigger()

    def get(self):
        return self._queue.popleft()
    
    def _trigger(self):
        self._invoke_listener.callback(None)
        self._invoke_listener = None

    def listen(self):
        if self._invoke_listener:
            raise Exception("Event stream in use from multiple places simultaneously.")
        d = self._invoke_listener = Deferred()
        if self._queue:
            self._trigger()
        return d # Using d because self._trigger() may change self._invoke_listener

    def stop_listen(self):
        assert self._invoke_listener or self._queue
        self._invoke_listener = None

class EventStreamFromDeferred(EventStream):
    def __init__(self, d = None):
        EventStream.__init__(self)
        if d is None:
            d = Deferred()
        self.deferred = d
        d.addCallback(self.put)

class Timeout(EventStream):
    def __init__(self, timeout):
        EventStream.__init__(self)
        h = reactor.callLater(timeout, self.put)
        def cancel():
            if not h.called:
                h.cancel()
        self.put.set_deleted_cb(cancel)

class Now(EventStream):
    def __init__(self):
        EventStream.__init__(self)
        self.put()

@inlineCallbacks
def select(*events):
    """Listens to the provided EventStreams, and returns a set of integers
    indicating which ones have data ready, as soon as there its at least
    one that is ready."""
    ready_list = []
    done = Deferred()
    def _select_cb(_, i):
        ready_list.append(i)
        if not done.called:
            done.callback(None)
    for i in range(len(events)):
        events[i].listen().addCallback(_select_cb, i)
    try:
        yield done
    finally:
        for e in events:
            e.stop_listen()
        del events # Needed to avoid creating a cycle when events gets put
                   # into the traceback associated with returnValue.
    returnValue(ready_list)

@inlineCallbacks
def switch(cases, multiple = False):
    events, actions = zip(*cases.iteritems())

    ready_list = yield select(*events)

    for i in ready_list:
        args, kwargs = events[i].get()
        if actions[i]:
            actions[i](*args, **kwargs)
        if not multiple:
            break

def wrap_function(f):
    def run_f(g):
        def run_g(*args, **kwargs):
            return f(g, *args, **kwargs)
        return run_g
    return run_f

@wrap_function
def async_test(f, *args, **kwargs):
    "Starts an asynchronous test, waits for it to complete, and returns its result."
    result = []
    def cb(value, good):
        result.append(good)
        result.append(value)
    inlineCallbacks(f)(*args, **kwargs).addCallbacks(callback = cb, callbackArgs = [True],
                                    errback  = cb, errbackArgs  = [False])
    while not result:
        reactor.iterate(0.02)
    if result[0]:
        # Uncomment the following line to check that all the tests
        # really are being run to completion.
        #raise(Exception("Success"))
        return result[1]
    else:
        result[1].raiseException()

def unittest_with_reactor(run_ros_tests):
    exitval = []
    def run_test():
        try:
            if len(sys.argv) > 1 and sys.argv[1].startswith("--gtest_output="):
                import roslib; roslib.load_manifest('multi_interface_roam')
                import rostest
                run_ros_tests()
            else:
                import unittest
                unittest.main()
            exitval.append(0)
        except SystemExit, v:
            exitval.append(v.code)
        except:
            import traceback
            traceback.print_exc()
        finally:
            reactor.stop()

    reactor.callWhenRunning(run_test)
    reactor.run()
    sys.exit(exitval[0])

if __name__ == "__main__":
    import unittest
    import threading
    import sys
    import gc
    from twisted.internet.defer import setDebugging
    setDebugging(True)

    class EventStreamTest(unittest.TestCase):
        def test_dies_despite_cb(self):
            """Test that EventStream gets unallocated despite its callback
            being held. As without another reference, calling the callback
            will have no effect."""
            es = EventStream()
            esr = weakref.ref(es)
            putter = es.put
            l = []
            putter.set_deleted_cb(lambda : l.append('deleted'))
            self.assertEqual(l, [])
            self.assertEqual(esr(), es)
            del es
            self.assertEqual(l, ['deleted'])
            self.assertEqual(esr(), None)

        @async_test
        def test_timeout_memory_frees_correctly(self):
            """Had a lot of subtle bugs getting select to free up properly.
            This test case is what pointed me at them."""
            before = 0
            after = 0

            # First couple of times through seems to create some new data.
            yield select(Timeout(0.001))
            yield select(Timeout(0.001))
            
            before = len(gc.get_objects())
            # The yielding statement seems necessary, some stuff only gets
            # cleaned up when going through the reactor main loop.
            yield select(Timeout(0.001))
            after = len(gc.get_objects())
            
            self.assertEqual(before, after)
            
        @async_test
        def test_timeout_autocancel(self):
            self.assertEqual(len(reactor.getDelayedCalls()), 0)
            yield select(Timeout(0.3), Timeout(0.001))
            self.assertEqual(len(reactor.getDelayedCalls()), 0)
            

    def follow_back(a, n):
        """Handy function for seeing why an object is still live by walking
        back through its referrers."""
        import inspect
        def print_elem(e):
            print repr(e)[0:200]
            try:
                print e.f_lineno
                #print e.f_locals
                #print dir(e)
            except:
                pass
        print
        print "Follow back:"
        print_elem(a)
        for i in range(n):
            r = gc.get_referrers(a)
            r.remove(inspect.currentframe())
            print
            print len(r)
            for e in r:
                print_elem(e)
            a = r[0]

    def compare_before_after(before, after):
        """Handy function to compare live objects before and after some
        operation, to debug why memory is leaking."""
        beforeids = set(id(e) for e in before)
        afterids = set(id(e) for e in after)
        delta = afterids - beforeids - set([id(before)])
        for e in after:
            if id(e) in delta:
                print e

    class SelectTest(unittest.TestCase):
        @async_test
        def test_select_param1(self):
            "Tests that the first parameter can get returned."
            self.assertEqual((yield (select(Timeout(.01), Timeout(100)))), [0])
        
        @async_test
        def test_select_param2(self):
            "Tests that the second parameter can get returned."
            self.assertEqual((yield (select(Timeout(100), Timeout(.01)))), [1])
        
        @async_test
        def test_select_both(self):
            "Tests that the both parameters can get returned."
            es1 = EventStream()
            es2 = EventStream()
            es1.put(None)
            es2.put(None)
            self.assertEqual((yield select(es1, es2)), [0, 1])
     
    class SwitchTest(unittest.TestCase):
        @async_test
        def test_switch_param(self):
            "Tests switch on single outcome."
            yield switch({
                Timeout(.01): lambda : None,
                Timeout(100): lambda : self.fail('Wrong switch'),
                })

        @async_test
        def test_switch_empty_action(self):
            "Tests that a None action doesn't cause an exception."
            yield switch({
                Timeout(.01): None,
                })

        @async_test
        def test_switch_both_single(self):
            "Tests switch on simultaneous, non-multiple."
            es1 = EventStream()
            es2 = EventStream()
            es1.put()
            es2.put()
            hits = []
            yield switch({
                es1: lambda : hits.append(None),
                es2: lambda : hits.append(None),
                    })
            self.assertEqual(len(hits), 1)
        
        @async_test
        def test_switch_both_multiple(self):
            "Tests switch on simultaneous, multiple."
            es1 = EventStreamFromDeferred()
            es2 = EventStreamFromDeferred()
            es1.deferred.callback(None)
            es2.deferred.callback(None)
            hits = []
            yield switch({
                es1: lambda _: hits.append(None),
                es2: lambda _: hits.append(None),
                    }, multiple = True)
            self.assertEqual(len(hits), 2)

        @async_test
        def test_switch_parameters(self):
            "Tests that switch passes parameters correctly."
            es = EventStream()
            es.put(3)
            es.put(4)
            yield switch({
                es: lambda v: self.assertEqual(v, 3),
                    }, multiple = True)
            yield switch({
                es: lambda v: self.assertEqual(v, 4),
                    }, multiple = True)
            self.assertRaises(IndexError, es._queue.pop)

    def run_ros_tests():
        rostest.unitrun('multi_interface_roam', 'eventstream', EventStreamTest)
        rostest.unitrun('multi_interface_roam', 'select', SelectTest)
        rostest.unitrun('multi_interface_roam', 'switch', SwitchTest)

    unittest_with_reactor(run_ros_tests)