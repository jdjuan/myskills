"""Small native adapters for recoverable deletion and safe usage-log files."""
import ctypes
import os
from pathlib import Path
import stat


def linked(path):
    info = Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def log_fd(path, write=False):
    path = Path(path)
    if os.name != 'nt':
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT if write else os.O_RDONLY
        return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    # OPEN_REPARSE_POINT opens the link itself so it can be rejected before reading/writing.
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0xC0000000 if write else 0x80000000, 7, None,
                                4 if write else 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    class AttributeTag(ctypes.Structure):
        _fields_ = [('attributes', wintypes.DWORD), ('tag', wintypes.DWORD)]
    info = AttributeTag()
    if not kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or info.attributes & 0x410:
        kernel.CloseHandle(handle)
        raise OSError('Usage log must be a regular file, not a linked location.')
    try:
        return msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
    except Exception:
        kernel.CloseHandle(handle)
        raise


def recycle_windows(folders):
    """The shell moves all requested paths to the user's Recycle Bin."""
    from ctypes import wintypes
    class FileOperation(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT),
                    ('pFrom', wintypes.LPCWSTR), ('pTo', wintypes.LPCWSTR),
                    ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHFileOperationW.argtypes = [ctypes.POINTER(FileOperation)]
    shell.SHFileOperationW.restype = ctypes.c_int
    operation = FileOperation()
    operation.wFunc = 3
    operation.pFrom = '\0'.join(str(Path(p).resolve()) for p in folders) + '\0\0'
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # allow undo, quiet, no confirmation/error UI
    result = shell.SHFileOperationW(ctypes.byref(operation))
    if result or operation.fAnyOperationsAborted:
        raise OSError('Windows could not move all selected folders to the Recycle Bin.')
