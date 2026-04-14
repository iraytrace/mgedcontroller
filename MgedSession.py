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
            print(f"Failed to launch: {e}")
            return

        self.thread = threading.Thread(target=self.thread_func,
                                    args=(self.process.stdout,
                                            self.output))
        self.thread.daemon = True
        self.thread.start()


        # wait for process to start responding

        mged.send_command(f'puts "hello"')

        good = False
        seconds = 0
        while self.process_running and self.thread_running and self.pending_commands > 0:
            if mged.command_completed:
                good = True
                break
            else:
                print(f"waiting for mged hello {seconds}: No output received. Process running: {self.process_running}, Thread alive: {self.thread_running}")
                seconds += 1
                time.sleep(1)

        if not good:
            print('mged launch failure')
            sys.exit(1)

    def send_command(self, command):
        if mged.process.poll() is not None:
            print('process stopped')
            return

        self.process.stdin.write(command + '\n')
        self.process.stdin.flush()
        self.in_flight += 1

    def shutdown(self):
        if self.process.poll() is None:

            print("Closing stdin and terminating process...")
            self.process.stdin.close()
            self.process.terminate() # or process.kill()
            self.process.wait(timeout=2)

        self.thread.join(timeout=2)


if __name__ == '__main__':
    mged_exe = shutil.which('mged.exe')
    mged = MgedSession(mged_exe)
    mged.launch()


    for i in range(10):
        if i < 3:
            mged.send_command(f'puts "hello {i}"')
        
        if mged.command_completed:
            while mged.command_completed:
                print(f'OUTPUT {i}: {mged.get_output()}')
        else:
            # This is expected if there's no output during the 1-second timeout
            print(f"Loop {i}: No output received. {mged.pending_commands} pending.\n\tProcess running: {mged.process.poll() is None}, Thread alive: {mged.thread.is_alive()}")
            time.sleep(1)
        # Cleanly close the process

    if mged.in_flight > 0:
        print(f'{mged.in_flight} still in flight')

    mged.shutdown()