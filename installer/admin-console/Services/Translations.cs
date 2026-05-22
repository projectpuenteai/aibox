using System.Collections.Generic;

namespace AIBox.AdminConsole.Services;

/// <summary>
/// Static ES/EN string table. Keys mirror $Text in aibox_control_ui.ps1
/// so prefs and behavior stay consistent between the two consoles.
/// </summary>
public static class Translations
{
    public const string LangEs = "es";
    public const string LangEn = "en";

    private static readonly Dictionary<string, Dictionary<string, string>> _table = new()
    {
        [LangEs] = new()
        {
            ["title"]          = "Consola Puente Admin",
            ["subtitle"]       = "Panel de control",
            ["tabEs"]          = "Español",
            ["tabEn"]          = "English",
            ["start"]          = "Iniciar",
            ["starting"]       = "Iniciando...",
            ["stop"]           = "Detener",
            ["stopping"]       = "Deteniendo...",
            ["ssid"]           = "Red Wi-Fi",
            ["password"]       = "Contraseña",
            ["ipv4"]           = "Dirección IP",
            ["ipUnavailable"]  = "Inicia el sistema",
            ["pillOff"]        = "APAGADO",
            ["pillStarting"]   = "INICIANDO...",
            ["pillReady"]      = "ENCENDIDO",
            ["pillStopping"]   = "DETENIENDO...",
            ["footerReady"]    = "Listo — {0}",
            ["footerOff"]      = "Sistema apagado",
            ["footerStarting"] = "Iniciando el sistema...",
            ["footerStopping"] = "Deteniendo el sistema...",
            ["footerError"]    = "Error: {0}",
            ["confirmCloseTitle"] = "Cerrar Consola Puente",
            ["confirmCloseBody"]  = "El sistema está en proceso. Si cierras la ventana, los comandos continuarán en segundo plano.\n\n¿Cerrar de todos modos?",
            ["consoleTitle"]      = "Salida del sistema",
            ["clearBtn"]          = "Limpiar",
            ["hotspotNotDetected"] = "El hotspot no se detectó tras 30 s. Revisa la salida abajo para ver los detalles.",
            ["scriptFailed"]       = "El script terminó con error: {0}",
        },
        [LangEn] = new()
        {
            ["title"]          = "Puente Admin Console",
            ["subtitle"]       = "Control panel",
            ["tabEs"]          = "Español",
            ["tabEn"]          = "English",
            ["start"]          = "Start",
            ["starting"]       = "Starting...",
            ["stop"]           = "Stop",
            ["stopping"]       = "Stopping...",
            ["ssid"]           = "Wi-Fi network",
            ["password"]       = "Password",
            ["ipv4"]           = "IP address",
            ["ipUnavailable"]  = "Start the system",
            ["pillOff"]        = "OFF",
            ["pillStarting"]   = "STARTING...",
            ["pillReady"]      = "ON",
            ["pillStopping"]   = "STOPPING...",
            ["footerReady"]    = "Ready — {0}",
            ["footerOff"]      = "System off",
            ["footerStarting"] = "Starting the system...",
            ["footerStopping"] = "Stopping the system...",
            ["footerError"]    = "Error: {0}",
            ["confirmCloseTitle"] = "Close Puente Console",
            ["confirmCloseBody"]  = "The system is in progress. If you close the window, the commands will keep running in the background.\n\nClose anyway?",
            ["consoleTitle"]      = "System output",
            ["clearBtn"]          = "Clear",
            ["hotspotNotDetected"] = "Hotspot did not come up after 30 s. Check the output below for details.",
            ["scriptFailed"]       = "Script exited with error: {0}",
        },
    };

    public static string T(string language, string key)
    {
        if (_table.TryGetValue(language, out var lang) && lang.TryGetValue(key, out var value))
            return value;
        if (_table[LangEn].TryGetValue(key, out var fallback))
            return fallback;
        return key;
    }

    public static string TF(string language, string key, params object[] args)
    {
        var template = T(language, key);
        try { return string.Format(template, args); }
        catch { return template; }
    }
}
