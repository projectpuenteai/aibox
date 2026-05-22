// nvidia-probe — exits 0 if an NVIDIA GPU is present, 1 if not.
//
// Detection strategy (in order):
//   1. Run `nvidia-smi --query-gpu=name --format=csv,noheader` and check
//      for at least one non-empty output line (fastest; works when the NVIDIA
//      driver is fully installed).
//   2. P/Invoke LoadLibrary("nvcuda.dll") — present whenever the CUDA runtime
//      DLL is installed in System32, even if nvidia-smi is not on PATH.
//
// Exit codes: 0 = NVIDIA GPU detected, 1 = not detected.

using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

namespace NvidiaProbe;

internal static partial class Program
{
    // P/Invoke for CUDA DLL presence check (fallback path).
    [LibraryImport("kernel32.dll", EntryPoint = "LoadLibraryW", StringMarshalling = StringMarshalling.Utf16)]
    private static partial IntPtr LoadLibrary(string lpFileName);

    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool FreeLibrary(IntPtr hModule);

    internal static int Main()
    {
        if (TryNvidiaSmi())
        {
            Console.Error.WriteLine("[nvidia-probe] nvidia-smi reported GPU present.");
            return 0;
        }

        if (TryNvCuda())
        {
            Console.Error.WriteLine("[nvidia-probe] nvcuda.dll found in System32 — GPU present.");
            return 0;
        }

        Console.Error.WriteLine("[nvidia-probe] No NVIDIA GPU detected.");
        return 1;
    }

    /// <summary>
    /// Runs nvidia-smi and checks for at least one non-blank output line.
    /// Returns false on any exception (tool not found, timeout, etc.).
    /// </summary>
    private static bool TryNvidiaSmi()
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = "nvidia-smi",
                Arguments = "--query-gpu=name --format=csv,noheader",
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };

            using var proc = Process.Start(psi);
            if (proc is null)
                return false;

            // Read output synchronously; nvidia-smi is fast.
            string stdout = proc.StandardOutput.ReadToEnd();
            proc.WaitForExit(10_000);

            if (proc.ExitCode != 0)
                return false;

            // Accept any non-whitespace line as a detected GPU name.
            foreach (var line in stdout.Split('\n', StringSplitOptions.RemoveEmptyEntries))
            {
                if (!string.IsNullOrWhiteSpace(line))
                    return true;
            }

            return false;
        }
        catch
        {
            // nvidia-smi not on PATH or OS-level failure — fall through to DLL check.
            return false;
        }
    }

    /// <summary>
    /// Attempts to load nvcuda.dll via LoadLibrary. The DLL is present in
    /// System32 on any machine with an NVIDIA CUDA-capable driver installed,
    /// even when nvidia-smi is not on PATH.
    /// </summary>
    private static bool TryNvCuda()
    {
        if (!RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
            return false;

        try
        {
            IntPtr handle = LoadLibrary("nvcuda.dll");
            if (handle == IntPtr.Zero)
                return false;

            FreeLibrary(handle);
            return true;
        }
        catch
        {
            return false;
        }
    }
}
