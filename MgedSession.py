"""
A standalone Python module for managing an external MGED process.

This module provides a generic, callback-based system for sending commands
and receiving their complete output asynchronously.
"""
import subprocess
import threading
import queue
import os
import time
import sys
import shutil

class MgedException(Exception):
    """Exception raised for talking with mged"""
    pass

class MgedSession:
    def __init__(self, mged_path):
        self.mged_path = mged_path
        self.output = queue.Queue()
        self.process = None
        self.thread = None
        self.mged_prompt = 'MGED_CMD_DONE'
        self.in_flight = 0  # number of commands without response

    @property
    def pending_commands(self):
        return self.in_flight
    
    @property
    def command_completed(self):
        data = [x.strip() for x in list(self.output.queue)]
        return any(s.startswith(self.mged_prompt) for s in data)

    @property
    def running(self):
        return bool(self.process_running and self.thread_running)

    @property
    def process_running(self):
        return bool(self.process is not None and self.process.poll() is None)

    @property
    def thread_running(self):
        return bool(self.thread is not None and self.thread.is_alive())

    @staticmethod
    def thread_func(process_stdout, out_q):
        for line in iter(process_stdout.readline, b''):
            out_q.put(line)
        process_stdout.close()


    def get_output(self):  # get output from a single mged command

        if not self.command_completed: # make sure we have at least one full output
            return ''

        data = []
        line = ''
        while not line.startswith(self.mged_prompt):
            line = self.output.get_nowait().strip()
            if not line.startswith(self.mged_prompt):
                data.append(line)

        self.in_flight -= 1
        return '\n'.join(data)


    def launch(self):
        commandline = [self.mged_path, '-c', '-p', '-a', 'wgl', 'd:/box.g']
        try:
            self.process = subprocess.Popen(commandline, 
                stdin=subprocess.PIPE, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1, # line buffering
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except Exception as e:
            raise MgedException(f"Mged failed to launch: {commandline} : {e}")

        try:
            self.thread = threading.Thread(target=self.thread_func,
                                        args=(self.process.stdout,
                                                self.output))
            self.thread.daemon = True
            self.thread.start()
        except Exception as e:
            raise MgedException("MgedSession failed to start thread")

        # wait for process to start responding
        self.send_command(f'puts "hello"')

        mged_has_responded = False
        seconds = 1
        time.sleep(1) # give subprocess a chance to get started

        while self.running and self.pending_commands > 0 and seconds < 15:
            if self.command_completed:
                mged_has_responded = True
                break

            print(f"Waiting for mged launch {seconds}s")
            seconds += 1
            time.sleep(1) # give subprocess a chance to get started

        if not mged_has_responded:
            raise MgedException('mged launch failure after {seconds} seconds.\n' +
                                ' Process: {self.process_running}\n' +
                                ' Thread: {self.thread_running}\n' +
                                ' Commands Pending: {self.pending_commands}')

    def send_command(self, command):
        if self.process is None:
            raise MgedException("No mged session {self.process}")
        if self.process.poll() is not None:
            raise MgedException("Mged process stopped")
        if self.thread is None or not self.thread_running:
            raise MgedException("No read thread")

        self.process.stdin.write(command + '\n')
        self.process.stdin.flush()
        self.in_flight += 1

    def shutdown(self):

        if self.process.poll() is None:
            self.send_command('q')  # attempt shutdown
            self.process.stdin.close()

            for seconds in range(3): # wait 3 seconds for mged to quit
                if self.process.poll() is None:
                    print(f'waiting for mged to quit {seconds}')
                    time.sleep(1)

            if self.process.poll() is None:
                # still running
                print("terminating process...")
                self.process.terminate() # or process.kill()
                self.process.wait(timeout=2)
            else:
                print(f'mged process done {self.process.poll()}')
        self.thread.join(timeout=2)

def main():
    mged_exe = shutil.which('mged.exe')
    if not mged_exe or not os.path.exists(mged_exe):
        print('no mged executable found: {mged_exe}')
        return

    mged = MgedSession(mged_exe)
    mged.launch()


    for i in range(10):
        if i < 3:
            mged.send_command(f'puts "hello {i}"')
        
        if mged.command_completed:
            while mged.command_completed:
                print(f'OUTPUT {i}: {mged.get_output()}')
        elif mged.pending_commands:
            print(f"Loop {i}: No output received. {mged.pending_commands} pending.\n\tProcess running: {mged.process.poll() is None}, Thread alive: {mged.thread.is_alive()}")
            time.sleep(1)
        # Cleanly close the process

    if mged.in_flight > 0:
        print(f'{mged.in_flight} still in flight')

    mged.shutdown()

        
if __name__ == '__main__':
    main()