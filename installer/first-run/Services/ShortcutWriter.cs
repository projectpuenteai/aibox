using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

namespace AIBox.FirstRun.Services;

/// <summary>
/// Writes .lnk shortcuts via the Windows IShellLink COM interface. We
/// avoid taking a WSH dependency because it isn't reliably available
/// from a self-contained .NET process. The shortcut we produce is the
/// same shape as the existing aibox_control_ui.ps1 desktop shortcut
/// (PowerShell launcher with the script path and "Run as admin" flag).
/// </summary>
public static class ShortcutWriter
{
    public static void WriteControlPanelShortcut(string lnkPath, string installRoot, FileLogger log)
    {
        var script = Path.Combine(installRoot, "aibox", "tools", "llama-runtime", "scripts", "aibox_control_ui.ps1");
        if (!File.Exists(script))
        {
            log.Warn($"aibox_control_ui.ps1 not found at {script}; shortcut will still be written but won't launch.");
        }

        WriteLnk(
            lnkPath: lnkPath,
            target: "powershell.exe",
            args: $"-NoProfile -ExecutionPolicy Bypass -File \"{script}\"",
            workingDir: Path.GetDirectoryName(script) ?? installRoot,
            description: "AIBox Control Panel — Start, Pause, and Stop the local AI stack.",
            iconPath: Path.Combine(installRoot, "aibox", "installer", "first-run", "Resources", "app.ico"),
            runAsAdmin: true);

        log.Info($"Wrote Control Panel shortcut at {lnkPath}.");
    }

    public static void DeleteShortcut(string lnkPath)
    {
        try { if (File.Exists(lnkPath)) File.Delete(lnkPath); } catch { /* best-effort */ }
    }

    private static void WriteLnk(
        string lnkPath, string target, string args, string workingDir,
        string description, string iconPath, bool runAsAdmin)
    {
        var shellLink = (IShellLinkW)new ShellLink();
        shellLink.SetPath(target);
        shellLink.SetArguments(args);
        shellLink.SetWorkingDirectory(workingDir);
        shellLink.SetDescription(description);
        if (File.Exists(iconPath))
            shellLink.SetIconLocation(iconPath, 0);

        var persist = (IPersistFile)shellLink;
        persist.Save(lnkPath, false);

        if (runAsAdmin)
            SetRunAsAdminBit(lnkPath);
    }

    private static void SetRunAsAdminBit(string lnkPath)
    {
        // Flip byte 21 of the LinkFlags-controlled header to set the
        // RunAsUser flag. See MS-SHLLINK §2.1.1 "LinkFlags" + 2.1.2
        // "DataFlags": offset 0x15 (21) bit 0x20. The SHLLINK_HEADER is
        // 0x4C (76) bytes minimum; byte 21 falls safely within this header.
        var bytes = File.ReadAllBytes(lnkPath);
        if (bytes.Length < 22) return;
        bytes[21] |= 0x20;
        File.WriteAllBytes(lnkPath, bytes);
    }

    // ---- minimal COM interop for IShellLinkW ----

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    private interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszFile,
            int cchMaxPath, IntPtr pfd, uint fFlags);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszName, int cchMaxName);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszDir, int cchMaxPath);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string pszDir);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszArgs, int cchMaxPath);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string pszArgs);
        void GetHotkey(out short pwHotkey);
        void SetHotkey(short wHotkey);
        void GetShowCmd(out int piShowCmd);
        void SetShowCmd(int iShowCmd);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszIconPath,
            int cchIconPath, out int piIcon);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string pszIconPath, int iIcon);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string pszPathRel, uint dwReserved);
        void Resolve(IntPtr hwnd, uint fFlags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string pszFile);
    }
}
