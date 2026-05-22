using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Detects whether the AIBox stack is "live" by looking for a connected
/// network adapter holding an IPv4 address in the 192.168.137.0/24 range —
/// the Windows Mobile Hotspot subnet, which only exists when up_stack.ps1
/// has finished. Mirrors Get-HostIPv4 in aibox_control_ui.ps1.
/// </summary>
public static class StackStatusProbe
{
    public sealed record Status(bool IsLive, string? HotspotIp);

    public static Status Probe()
    {
        try
        {
            var interfaces = NetworkInterface.GetAllNetworkInterfaces();
            foreach (var nic in interfaces)
            {
                if (nic.OperationalStatus != OperationalStatus.Up) continue;

                var ipProps = nic.GetIPProperties();
                foreach (var addr in ipProps.UnicastAddresses)
                {
                    if (addr.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                    var ip = addr.Address.ToString();
                    if (ip.StartsWith("192.168.137.", System.StringComparison.Ordinal))
                        return new Status(true, ip);
                }
            }
        }
        catch
        {
            // Treat probe failure as "not live"; the timer will retry.
        }

        return new Status(false, null);
    }
}
