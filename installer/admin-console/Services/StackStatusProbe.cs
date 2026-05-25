using System;
using System.Diagnostics;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Detects whether the AIBox stack is "live".
///
/// Docker is the source of truth for ON/OFF: the system is considered running
/// when at least one <c>aibox-*</c> container is up. The Mobile Hotspot is
/// tracked separately — its 192.168.137.0/24 host address only exists when the
/// hotspot is actually broadcasting — so the UI can show the IP when it's up or
/// flag "HOTSPOT NOT ACTIVE" when Docker is running without it.
/// </summary>
public static class StackStatusProbe
{
    public sealed record Status(bool DockerRunning, bool HotspotUp, string? HotspotIp)
    {
        /// <summary>The system is "on" iff Docker is running.</summary>
        public bool IsLive => DockerRunning;
    }

    /// <summary>
    /// Synchronous, in-process scan for a connected adapter holding an IPv4
    /// address in the Windows Mobile Hotspot subnet (192.168.137.0/24).
    /// Mirrors Get-HostIPv4 in aibox_control_ui.ps1. Returns instantly.
    /// </summary>
    public static (bool Up, string? Ip) ProbeHotspot()
    {
        try
        {
            foreach (var nic in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (nic.OperationalStatus != OperationalStatus.Up) continue;

                foreach (var addr in nic.GetIPProperties().UnicastAddresses)
                {
                    if (addr.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                    var ip = addr.Address.ToString();
                    if (ip.StartsWith("192.168.137.", StringComparison.Ordinal))
                        return (true, ip);
                }
            }
        }
        catch
        {
            // Treat probe failure as "not up"; the timer will retry.
        }

        return (false, null);
    }

    /// <summary>
    /// Asks the Docker CLI whether any aibox-* container is running. Returns
    /// false on any launch failure, non-zero exit, or timeout so a daemon that
    /// is down or still starting can't wedge the UI. <c>docker ps</c> lists only
    /// running containers by default; <c>docker</c> is on PATH (the up/down
    /// scripts invoke it bare).
    /// </summary>
    public static async Task<bool> ProbeDockerAsync(CancellationToken ct)
    {
        try
        {
            var psi = new ProcessStartInfo("docker")
            {
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            psi.ArgumentList.Add("ps");
            psi.ArgumentList.Add("--filter");
            psi.ArgumentList.Add("name=aibox-");
            psi.ArgumentList.Add("--format");
            psi.ArgumentList.Add("{{.Names}}");

            using var proc = new Process { StartInfo = psi };
            if (!proc.Start()) return false;

            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(6));

            var readStdout = proc.StandardOutput.ReadToEndAsync();

            try
            {
                await proc.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                try { proc.Kill(true); } catch { /* best effort */ }
                return false;
            }

            if (proc.ExitCode != 0) return false;

            var output = await readStdout.ConfigureAwait(false);
            foreach (var line in output.Split('\n'))
            {
                if (line.TrimStart().StartsWith("aibox-", StringComparison.Ordinal))
                    return true;
            }
        }
        catch
        {
            // docker not found, daemon unreachable, etc. → treat as not running.
        }

        return false;
    }

    /// <summary>Combines the (sync) hotspot scan and (async) Docker check.</summary>
    public static async Task<Status> ProbeAsync(CancellationToken ct = default)
    {
        var (hotspotUp, hotspotIp) = ProbeHotspot();
        var dockerRunning = await ProbeDockerAsync(ct).ConfigureAwait(false);
        return new Status(dockerRunning, hotspotUp, hotspotIp);
    }
}
