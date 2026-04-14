import subprocess
import threading
import queue
import os
import time
import shutil

class MgedException(Exception):
    """Exception raised for talking with mged"""
    pass

class MgedCommand:
    """A handle for a command sent to MGED."""
    def __init__(self, text):
        self.text = text
        self._result = None
        self._completed = threading.Event()

    @property
    def is_done(self):
        return self._completed.is_set()

    def result(self, timeout=None):
        """Wait for and return the output of the command."""
        if self._completed.wait(timeout):
            return self._result
        return None

    def _set_result(self, output):
        self._result = output
        self._completed.set()

class MgedSession:
    def __init__(self, mged_path, command_line_options=[]):
        self.mged_path = mged_path
        self.process = None
        self.thread = None
        self.command_line_options = command_line_options
        self.mged_prompt = 'MGED_CMD_DONE'

        self._pending_futures = queue.Queue()

    @property
    def running(self):
        return bool(self.process and self.process.poll() is None)

    def _manager_thread(self):
        """
        Reads stdout line by line. When a prompt is found, 
        it fulfills the oldest pending MgedCommand.
        """
        buffer = []
        try:
            for line in iter(self.process.stdout.readline, ''):
                clean_line = line.strip()
                
                if clean_line.startswith(self.mged_prompt):
                    # End of a command reached
                    output = "\n".join(buffer)
                    buffer = []
                    
                    try:
                        # Get the future associated with this output
                        future = self._pending_futures.get_nowait()
                        future._set_result(output)
                    except queue.Empty:
                        pass # Spontaneous output or setup output
                else:
                    buffer.append(clean_line)
        finally:
            self.process.stdout.close()

    def launch(self, db_path=None):
        commandline = [self.mged_path] + self.command_line_options
        if db_path:
            commandline.append(db_path)

        print(f'Launch: {commandline}')
        try:
            self.process = subprocess.Popen(
                commandline, 
                stdin=subprocess.PIPE, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            self.thread = threading.Thread(target=self._manager_thread, daemon=True)
            self.thread.start()
            
            # Initial handshake
            init_cmd = self.send_command('puts "hello"')
            if init_cmd.result(timeout=10) is None:
                raise MgedException("MGED failed to respond to handshake.")
                
        except Exception as e:
            raise MgedException(f"Mged failed to launch: {e}")

    def send_command(self, command_text):
        if not self.running:
            raise MgedException("MGED process is not running.")

        future = MgedCommand(command_text)
        self._pending_futures.put(future)
        
        full_command = f"{command_text}\n"
        
        self.process.stdin.write(full_command)
        self.process.stdin.flush()
        return future

    def shutdown(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.stdin.write("q\n")
                self.process.stdin.flush()
            except:
                pass
            
            self.process.wait(timeout=3)
            if self.process.poll() is None:
                self.process.terminate()

def main():
    mged_exe = shutil.which('mged') # or full path
    if not mged_exe:
        print("mged not found")
        return

    session = MgedSession(mged_exe, ['-c', '-p', '-a', 'wgl'])
    session.launch('d:/box.g')

    # Track specific commands
    commands = ['attach wgl', 'title', 'e asdf.r', 'tops']
    cmds = []
    for i in range(len(commands)):
        print(f"Sending command {i} {commands[i]}")
        cmds.append(session.send_command(commands[i]))

    time.sleep(10)
    # Check them later
    for idx, cmd in enumerate(cmds):
        # This will block until the specific command is done
        result = cmd.result(timeout=5) 
        print(f'Command {idx} "{cmd.text}" finished with output: {result}')

    session.shutdown()

if __name__ == '__main__':
    main()