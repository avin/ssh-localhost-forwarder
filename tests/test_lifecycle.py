"""A hard-killed forwarder must not leave its SSH children behind."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name == "nt", "Windows process ownership")
class ProcessLifecycleTests(unittest.TestCase):
    def test_hard_kill_terminates_owned_children(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        code = """
import subprocess, sys, time
import forwarder
job = getattr(forwarder, 'protect_process_tree', lambda: None)()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(child.pid, flush=True)
time.sleep(60)
"""
        parent = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True,
                                  cwd=Path(__file__).resolve().parents[1])
        child_pid = None
        child_handle = None
        try:
            line = parent.stdout.readline().strip()
            self.assertTrue(line.isdigit(), "Child owner failed to start: " + parent.stderr.read() if not line else line)
            child_pid = int(line)
            child_handle = kernel.OpenProcess(0x00100001, False, child_pid)
            self.assertTrue(child_handle, "Child process must be running before the crash")
            parent.kill()
            parent.wait(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 5000), 0,
                             "A hard-killed owner left a child process running")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            parent.stdout.close()
            parent.stderr.close()
            if child_handle:
                if kernel.WaitForSingleObject(child_handle, 0) != 0:
                    kernel.TerminateProcess(child_handle, 1)
                kernel.CloseHandle(child_handle)


if __name__ == "__main__":
    unittest.main()
