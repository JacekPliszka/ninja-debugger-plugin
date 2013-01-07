#!/usr/bin/env python
# -*- coding: utf-8 *-*
'''
This module provides objects to manage the execution of the debugger.
'''

import logging
import os
import process
import Queue
import serialize
from SimpleXMLRPCServer import SimpleXMLRPCServer
import sys
import threading
import time
import weakref


logger = logging.getLogger("ninja_debugger")

# Debugger internal data
_DEBUGGER_VERSION = "0.2"
_IGNORE_FILES = ['threading.py', 'process.py', 'ndb.py', 'serialize.py'
                 'weakref.py']

# States
STATE_INITIALIZED = "STATE_INITIALIZED"
STATE_RUNNING = "STATE_RUNNING"
STATE_PAUSED = "STATE_PAUSED"
STATE_TERMINATED = "STATE_TERMINATED"

# Commands
CMD_RUN = "Run"
CMD_STEP_OVER = "Over"
CMD_STEP_INTO = "Into"
CMD_STEP_OUT = "Out"


"""

INTERACTIONS

Operation               Workflow                Description
=========               ========                ===========
ping                    User -> Debugger        Returns the debugger version.

start                   User -> Debugger        Begins debugging execution.

stop                    User -> Debugger        Stops debugging execution.

list_threads            User -> Debugger        Returns the list of threads
                                                currently executing.
thread_started          Debugger -> User        Notifies about a new thread.

thread_ended            Debugger -> User        Notifies about a thread's end.

thread_suspended        Debugger -> User        Notifies that a thread has
                                                being stopped at a point.

resume                  User -> Debugger        Resumes execution of a stopped
                                                thread.

step_over               User -> Debugger        Steps over next statement.

step_into               User -> Debugger        Steps into next statement.

step_out                User -> Debugger        Steps out of current block of
                                                statements and to the outer 
                                                frame.

get_stack               User -> Debugger        Returns the stack trace for a
                                                particular thread.

set_breakpoint          User -> Debugger        Sets a stopping point.

evaluate                User -> Debugger        Returns the value of the
                                                expression when evaluated in
                                                the context of the specified
                                                thread.

get_messages            User -> Debugger        Returns the available messages
                                                from the debugger.


Workflow Example:


User Code               Debugger (ndb.py)
---------               -----------------
(executes debugger)

ping            ->      {version}

set_breakpoint  ->      'OK'

start           ->      'OK'

get_messages    ->      [ {msg1}, {msg2}, ...]
                        (threads created, suspended, ended, etc)

step_*          ->      {tid}

evaluate        ->      {evaluation result}

resume          ->      {tid}

get_messages    ->      [ {msg9}, {msg10}, ...]
                        (threads created, suspended, ended, etc)

resume_all      ->      'OK'

stop            ->      'OK'


"""

class DebugMessageFactory:

    MSG_NOP = 0x01
    MSG_THREAD_STARTED = 0x02
    MSG_THREAD_SUSPENDED = 0x03
    MSG_THREAD_ENDED = 0x04

    def __init__(self):
        self._counter = 0

    @staticmethod
    def makeNoOp():
        """Return a message with no operations. Null operation for testing."""
        return {'type': DebugMessageFactory.MSG_NOP, }
    
    @staticmethod
    def makeThreadStarted(thread_id):
        """
        Return a message with information about the thread that started its
        execution.
        """
        return { 'type': DebugMessageFactory.MSG_THREAD_STARTED,
                 'id': thread_id, }
    
    @staticmethod
    def makeThreadSuspended(thread_id, frame):
        """
        Return a message with information about the thread being paused and
        the position on which it is stopped.
        """
        f_path = frame.f_code.co_filename
        f_line = frame.f_lineno
        
        return { 'type': DebugMessageFactory.MSG_THREAD_SUSPENDED,
                 'id': thread_id,
                 'file': f_path,
                 'line':f_line, }
    
    @staticmethod
    def makeThreadEnded(thread_id):
        """
        Return a message with information about the thread that is ending its
        execution.
        """
        return { 'type': DebugMessageFactory.MSG_THREAD_ENDED,
                 'id': thread_id, }


class DebuggerInteractor(threading.Thread, SimpleXMLRPCServer):
    """
    """

    def __init__(self, debugger):
        """
        Create a new DebuggerInteractor instance. Allow external users
        to interact with the debugger through XML-RPC.
        """
        threading.Thread.__init__(self, name="DebuggerInteractor")
        SimpleXMLRPCServer.__init__(self, ("", 8765), logRequests=False)

        self._quit = False
        self._debugger = debugger

    def _dispatch(self, method, params):
        """
        Return the function associated for the method specified. Return the
        function starting with "export_" + method to prevent potential security
        problems.
        """
        try:
            func = getattr(self, 'export_' + method)
        except AttributeError:
            raise Exception('method "%s" is not supported' % method)
        else:
            return func(*params)

    def run(self):
        """Start request handling loop."""
        while not self._quit:
            self.handle_request()

    def quit(self):
        """Stop the request handling loop."""
        self._quit = True

    def export_ping(self):
        """Return the current debugger version."""
        return _DEBUGGER_VERSION

    def export_start(self):
        """Start the debugger session. Return 'OK' if everything is fine."""
        self._debugger.start()
        return "OK"

    def export_stop(self):
        """Stop debugger session. ."""
        self._debugger.stop()
        return "OK"

    def export_resume(self, t_id):
        """
        Resume execution of the specified thread. Stop execution only at
        breakpoints. Return the id of the thread resumed.
        """
        t = self._debugger.get_thread(t_id)
        t.resume()
        return str(t_id)

    def export_resume_all(self):
        """Resume execution of all threads."""
        self._debugger.resume()
        return 'OK'

    def export_step_over(self, t_id):
        """
        Resume execution of the specified thread, but stop at the next
        line in the current frame of execution.
        """
        t = self._debugger.get_thread(t_id)
        t.step_over()
        return str(t_id)

    def export_step_into(self, t_id):
        """
        Resume execution of the specified thread, but stop at the very next
        line of code, in or within the current frame.
        """
        t = self._debugger.get_thread(t_id)
        t.step_into()
        return str(t_id)

    def export_step_out(self, t_id):
        """
        Resume execution of the specified thread, but stop after the return of
        the current frame.
        """
        t = self._debugger.get_thread(t_id)
        t.step_out()
        return str(t_id)

    def export_get_stack(self, t_id):
        """Return the stack trace of the specified thread."""
        t_obj = self._debugger.get_thread(t_id)
        return t_obj.get_stack()

    def export_set_breakpoint(self, file, line):
        """Set the specified line in file as a breakpoint."""
        self._debugger.set_breakpoint(file, line)
        return self._debugger._breakpoints

    def export_evaluate(self, t_id, e_str):
        """
        Evaluate e_str in the context of the globals and locals from
        the execution frame in the specified thread.
        """
        t_obj = self._debugger.get_thread(t_id)
        result = t_obj.evaluate(e_str)
        return serialize.GenericSerializer.serialize(e_str, e_str, result)

    def export_execute(self, t_id, e_str):
        """
        Executes e_str in the context of the globals and locals from the
        execution frame in the specified thread.
        """
        t_obj = self._debugger.get_thread(t_id)
        result = t_obj.execute(e_str)
        return serialize.GenericSerializer.serialize(e_str, e_str, result)

    def export_list_threads(self):
        """
        List the running threads.
        """
        t_list = []
        for t_id in self._debugger._threads:
            t_obj = self._debugger._threads[t_id]
            t_name = t_obj._name
            t_state = t_obj._state
            t_list.insert(0, (t_id, t_name, t_state))
        return t_list
    
    def export_get_messages(self):
        return self._debugger.get_messages()


class DebuggerThread:
    """
    """

    def __init__(self, id, name, frame, debugger):
        """
        Create a new DebuggerThread from the starting frame with an id and a
        name.
        """
        self._id = id
        self._name = name
        self._f_origin = frame
        self._f_current = frame
        self._f_stop = None
        self._f_cmd = CMD_RUN
        self._state = STATE_RUNNING
        self._debugger = debugger
        
        # Notify about this thread being created
        msg = DebugMessageFactory.makeThreadStarted(self._id)
        self._debugger.put_message(msg)

        # Trace frame
        frame.f_trace = self._trace_dispatch

    def _trace_dispatch(self, frame, event, arg):
        """
        Analyze a given frame and event in the trace. Stop waiting for
        events when a stop is appropriate.
        """
        # If thread is "returning" then it might be ending, so, if the frame
        # from which we are "leaving" then we consider this thread ended.
        if event == 'return' and frame is self._f_origin:
            self.stop()
        
        if self._state == STATE_TERMINATED:
            return None

        # Set current frame
        self._f_current = frame

        # Get the "stop frame". This stop frame may not be the same as the one
        # we are "executing" for example for returns we stop on the caller.
        s_frame = self._stop_frame(frame, event)
        if s_frame:
            self._state = STATE_PAUSED
            
            # Notify about this thread being suspended
            msg = DebugMessageFactory.makeThreadSuspended(self._id, s_frame)
            self._debugger.put_message(msg)
            
            self._wait()

        # Return our trace function
        return self._trace_dispatch

    def _stop_frame(self, frame, event):
        """
        Return the corresponding stop frame for the current position (defined
        by frame) and event. Return None when we don't have to stop.
        """
        if event is 'return':
            # Depending on the kind of command we have, we should check if this
            # is a stopping point. Always return the upper frame on a return.
            if self._f_cmd is CMD_STEP_INTO:
                self._f_current = frame.f_back
                return frame.f_back
            if self._f_cmd in [CMD_STEP_OVER, CMD_STEP_OUT] and frame is self._f_stop:
                self._f_current = frame.f_back
                return frame.f_back

        if event is 'line':
            if self._f_cmd is CMD_STEP_INTO:
                return frame
            if self._f_cmd is CMD_STEP_OVER:
                if frame is self._f_stop:
                    return frame

        # If we've hit a breakpoint we should stop at the current frame
        f_path = frame.f_code.co_filename
        f_line = frame.f_lineno
        if self._debugger.is_breakpoint(f_path, f_line):
            return frame
        return None

    def _wait(self):
        """Stop the thread until the status change to other than PAUSED."""
        while self._state == STATE_PAUSED:
            time.sleep(0.1)

    def stop(self):
        """Make the current thread stop executing."""
        # Notify about this thread being terminated.
        msg = DebugMessageFactory.makeThreadEnded(self._id)
        self._debugger.put_message(msg)
            
        self._f_origin = None
        self._f_current = None # Release current frame
        self._f_stop = None
        self._f_cmd = None
        self._state = STATE_TERMINATED
        self._debugger = None

    def resume(self):
        """Make this thread resume execution after a stop."""
        self._f_stop = None
        self._f_cmd = CMD_RUN
        self._state = STATE_RUNNING
        return self._state

    def step_over(self):
        """Stop on the next line in the current frame."""
        self._f_stop = self._f_current
        self._f_cmd = CMD_STEP_OVER
        self._state = STATE_RUNNING

    def step_into(self):
        """Stop execution at the next line of code."""
        self._f_stop = None
        self._f_cmd = CMD_STEP_INTO
        self._state = STATE_RUNNING
        pass

    def step_out(self):
        """Stop execution after the return of the current frame."""
        self._f_stop = self._f_current
        self._f_cmd = CMD_STEP_OUT
        self._state = STATE_RUNNING

    def get_stack(self):
        """
        Return an array of tuples with the file names and line numbers of
        each entry in the stack. The first entry is the upper frame.
        """
        stack = []
        # Add all frames in the stack to the result
        index_f = self._f_current()
        while index_f is not None:
            f_name = os.path.basename(index_f.f_code.co_filename)
            f_line = index_f.f_lineno
            # Add only if the files isn't in our "blacklist"
            if not f_name in _IGNORE_FILES:
                stack.insert(0, (f_name, f_line))
            index_f = index_f.f_back
        return stack

    def get_frame(self):
        """Return the frame of current execution."""
        return self._f_current

    def evaluate(self, expr):
        """
        Evaluate an expression in the context of the current thread and return
        its value. The expression cannot contains assignments.
        """
        try:
            result = eval(expr, self._f_current.f_globals,
                          self._f_current.f_locals)
        except SyntaxError as serr:
            result = serr
        except Exception as err:
            result = err
        return result
    
    def execute(self, expr):
        """
        Execute an expression in the context of the current thread and return
        its value.
        """
        try:
            # Compile and execute code
            c_code = compile(source=expr, filename="<string>", mode='exec')
            exec c_code in self._f_current.f_globals, self._f_current.f_locals
            result = ""
        except SyntaxError as serr:
            result = serr
        except Exception as err:
            result = err
        return result


class Debugger:
    """
    Debugger Class
    """

    def __init__(self, s_file, state=STATE_PAUSED):
        """
        Creates a new Debugger. By default the debugger will start paused
        and waiting to be set on running.
        """
        self.s_file = s_file
        self._threads = weakref.WeakValueDictionary()
        self._messages = Queue.Queue()
        self._breakpoints = dict()      # _breakpoints[filename] = [line1, ...]
        self._state = state

    def start(self):
        """Start debugging session. Begin execution of the debugged code."""
        self._state = STATE_RUNNING
        return self._state
    
    def resume(self):
        """Resume execution on all the currently executing threads."""
        self._state = STATE_RUNNING
        # Terminate all current threads.
        for t_id in self._threads:
            self._threads[t_id].resume()
        return self._state

    def stop(self):
        """
        Stop execution of the debugged code. Terminate all running threads.
        """
        self._state = STATE_TERMINATED
        # Terminate all current threads.
        for t_id in self._threads:
            self._threads[t_id].stop()
        return self._state

    def run(self):
        """
        Starts execution of the script in a clean environment (or at least
        as clean as we can provide).
        """
        # Start communication interface (interactor)
        di = DebuggerInteractor(self)
        di.start()
        # Set script dirname as first lookup directory
        sys.path.insert(0, os.path.dirname(self.s_file))
        # Mainthread id
        mt_id = threading.currentThread().ident
        try:
            # Wait until we're ready to start
            while self._state != STATE_RUNNING:
                time.sleep(0.1)
            # Set tracing...
            threading.settrace(self.trace_dispatch)
            sys.settrace(self.trace_dispatch)
            # Execute file
            process.CodeExecutor(self.s_file).run()
            # UnSet tracing...
            threading.settrace(None)
            sys.settrace(None)
            # Wait for all threads to finish.
            while len(self._threads) > 0:
                time.sleep(0.1)
        finally:
            threading.settrace(None)
            sys.settrace(None)
            di.quit()
            self._state = STATE_TERMINATED

    def trace_dispatch(self, frame, event, arg):
        """
        Initial trace method. Create the DebuggerThread if it's a new thread
        or detour the trace to the corresponding thread.
        """
        if self._state == STATE_TERMINATED:
            return None

        if event not in ['call', 'line', 'return', 'exception']:
            return None

        filename = os.path.basename(frame.f_code.co_filename)
        if event == 'call' and filename in _IGNORE_FILES:
            return None

        # Get current thread id
        t_id = threading.currentThread().ident
        if not t_id in self._threads:
            t_name = threading.currentThread().name
            self._threads[t_id] = DebuggerThread(t_id, t_name, frame, self)
        else:
            # Redirect the trace to the thread's method
            return self._threads[t_id]._trace_dispatch
        return None

    def get_messages(self):
        """
        Return the debugger's available messages. Messages allow clients to
        know the current state of the debugging session.
        """
        result = []
        while not self._messages.empty():
            result.append(self._messages.get(block=True))
        return result

    def put_message(self, e):
        """
        Publish a new event on the event queue of this debugger. Events can be
        retrieved by the get_messages method.
        """
        self._messages.put(e)

    def get_thread(self, t_id):
        """
        Return the thread identified with t_id.
        """
        return self._threads[t_id]

    def set_breakpoint(self, file, line):
        """
        Add a breaking point in the debugging session in the specified file and
        line number. Debugging session will stop at that point waiting for user
        interaction.
        """
        fullpath = os.path.abspath(file)
        lines = self._breakpoints.setdefault(fullpath, [])
        if not line in lines:
            lines.append(line)

    def is_breakpoint(self, file, line):
        """
            Checks wheather the file:line is a break point. Returns True if it
            is, False otherwise.
        """
        fullpath = os.path.abspath(file)
        if fullpath in self._breakpoints:
            f_breaks = self._breakpoints.get(fullpath, [])
            return line in f_breaks
        return False


if __name__ == '__main__':
    if not sys.argv[1:]:
        print "File name is missing"
        raise SystemExit
    # Remove ourselves from the argv. (Try to be transparent to the script).
    del sys.argv[0]

    Debugger(sys.argv[0]).run()