import React, { useMemo, useState } from "react";
import {
  AlertTriangle,
  BarChart3,
  BookOpen,
  Circle,
  Expand,
  FileText,
  GraduationCap,
  KeyRound,
  Lock,
  LogOut,
  MessageSquare,
  Moon,
  Play,
  Power,
  RefreshCw,
  RotateCcw,
  Search,
  Settings,
  Shield,
  Shrink,
  Square,
  Sun,
  Trash2,
  Unlock,
} from "lucide-react";

const logoSrc = "/assets/circlelogo.png";

const INITIAL_ACCOUNTS = [
  {
    user: "guest-mnjuhfge-5nr5eu",
    role: "guest",
    storage: "0 bytes",
    restriction: "No active restrictions",
    locked: "No",
  },
  {
    user: "puenteAdmin",
    role: "admin",
    storage: "387,728 bytes",
    restriction: "No active restrictions",
    locked: "No",
  },
];

const STORAGE_LINES = {
  compact: [
    "Top Users By Storage",
    "- puenteAdmin (387,728 bytes)",
    "- guest-mnjuhfge-5nr5eu (0 bytes)",
    "",
    "Largest Documents",
    "- puenteAdmin: Rename ts | 410 bytes | type=markdown",
    "- puenteAdmin: Untitled Document | 366 bytes | type=markdown",
    "- puenteAdmin: sus boi | 362 bytes | type=markdown",
  ],
  expanded: [
    "- puenteAdmin: Reading Notes | 350 bytes | type=markdown",
    "- puenteAdmin: Guatemala Outline | 341 bytes | type=markdown",
    "",
    "Growth Trend",
    "- +14,220 bytes over the last 7 days",
    "- Docs are growing faster than chats",
    "",
    "Suggested Cleanup",
    "- Archive duplicate untitled documents",
    "- Review large admin chats for retention limits",
  ],
};

const SECURITY_LINES = {
  compact: [
    "[3/19/2026, 8:51:47 PM] warning admin_lock",
    "user=puenteAdmin ip=172.19.0.1",
    "endpoint=/v1/app/admin/users/53b87e73/unlock",
    "detail=Locked jack (I hate you)",
    "",
    "[3/19/2026, 5:09:50 PM] info admin_unlock",
    "user=puenteAdmin ip=172.19.0.1",
    "detail=Unlocked jack (i was mean)",
    "",
    "[3/19/2026, 5:09:42 PM] warning admin_lock",
    "user=puenteAdmin ip=172.19.0.1",
    "detail=Locked jack (too much data)",
  ],
  expanded: [
    "",
    "[3/18/2026, 3:54:13 PM] info admin_unlock",
    "user=puenteAdmin ip=172.19.0.1",
    "detail=Unlocked guest account after review",
    "",
    "Event Summary",
    "- 2 manual locks this week",
    "- 1 unlock after admin review",
    "- No system-wide abuse spike detected",
  ],
};

export default function ProjectPuentePortalRedesign() {
  const [themeMode, setThemeMode] = useState("light");
  const [viewMode, setViewMode] = useState("portal");
  const [expandedPanel, setExpandedPanel] = useState(null);
  const [runtimeStatus, setRuntimeStatus] = useState("good");
  const [activeRuntimeAction, setActiveRuntimeAction] = useState(null);
  const [accountRows, setAccountRows] = useState(INITIAL_ACCOUNTS);

  const theme = useMemo(() => {
    if (themeMode === "dark") {
      return {
        page: "bg-[linear-gradient(135deg,#0c1730_0%,#132742_52%,#0f1f36_100%)]",
        shell: "border-[#294669] bg-[#12233d]/82 shadow-[0_28px_80px_rgba(2,8,22,0.34)] backdrop-blur-xl",
        panel: "bg-[#162844] border-[#294669] shadow-[0_18px_48px_rgba(2,8,22,0.24)]",
        panelAlt: "bg-[#14263f] border-[#294669] shadow-[0_18px_48px_rgba(2,8,22,0.24)]",
        panelSoft: "bg-[#10213a] border-[#35557f]",
        text: "text-white",
        textSoft: "text-slate-300",
        textMuted: "text-slate-400",
        heroBadge: "border-[#2a4d7d] bg-[#173154] text-[#a8c8ff]",
        iconTile: "border-[#2a4d7d] bg-[#173154] text-[#a8c8ff]",
        neutralBtn: "border-[#294669] bg-[#162844] text-slate-100 hover:bg-[#1a3154]",
        neutralInput: "border-[#35557f] bg-[#10213a] text-white placeholder:text-slate-500",
        blueActive: "border-[#2f74db] bg-gradient-to-r from-[#4b84e4] to-[#2f74db] text-white shadow-[0_16px_32px_rgba(47,116,219,0.24)]",
        redActive: "border-[#6a3742] bg-[#3b1f25] text-[#ffb4ab] shadow-[0_16px_28px_rgba(60,20,28,0.24)]",
        greenActive: "border-[#235437] bg-[#11311e] text-[#86efac] shadow-[0_16px_28px_rgba(17,49,30,0.24)]",
        dangerSoft: "border-[#6a3742] bg-[#3b1f25] text-[#ffb4ab]",
        successPill: "border-[#235437] bg-[#11311e] text-[#86efac]",
        warningPill: "border-[#665013] bg-[#32280b] text-[#facc15]",
        stopPill: "border-[#6a3742] bg-[#3b1f25] text-[#ffb4ab]",
        restartPill: "border-[#2a4d7d] bg-[#173154] text-[#93c5fd]",
        code: "border-[#294669] bg-[#101d33] text-slate-100",
        glowA: "bg-[#2a66c8]/20",
        glowB: "bg-[#123b7a]/18",
        glowC: "bg-[#214f94]/16",
        logout: "border-[#6a3742] bg-[#3b1f25] text-[#ffb4ab] hover:bg-[#49262d]",
      };
    }

    return {
      page: "bg-[linear-gradient(135deg,#eaf2fb_0%,#e3edf9_52%,#d6e4f6_100%)]",
      shell: "border-white/70 bg-white/84 shadow-[0_24px_80px_rgba(27,76,138,0.10)] backdrop-blur-xl",
      panel: "bg-white border-slate-200 shadow-[0_18px_48px_rgba(27,76,138,0.08)]",
      panelAlt: "bg-[#f7faff] border-slate-200 shadow-[0_18px_48px_rgba(27,76,138,0.08)]",
      panelSoft: "bg-white border-slate-300",
      text: "text-slate-950",
      textSoft: "text-slate-600",
      textMuted: "text-slate-500",
      heroBadge: "border-[#cfe0fa] bg-[#edf4ff] text-[#1d4e96]",
      iconTile: "border-[#cfe0fa] bg-[#edf4ff] text-[#1d4e96]",
      neutralBtn: "border-slate-200 bg-[#eef4fb] text-slate-800 hover:bg-[#e6eef8]",
      neutralInput: "border-slate-300 bg-white text-slate-900 placeholder:text-slate-400",
      blueActive: "border-[#2f74db] bg-gradient-to-r from-[#4b84e4] to-[#2f74db] text-white shadow-[0_16px_32px_rgba(47,116,219,0.22)]",
      redActive: "border-[#f3c3ba] bg-[#feeceb] text-[#b42318] shadow-[0_16px_28px_rgba(180,35,24,0.12)]",
      greenActive: "border-[#b7e2c4] bg-[#e8f7ee] text-[#166534] shadow-[0_16px_28px_rgba(22,101,52,0.12)]",
      dangerSoft: "border-[#f3c3ba] bg-[#fff3f0] text-[#b42318]",
      successPill: "border-[#b7e2c4] bg-[#e8f7ee] text-[#166534]",
      warningPill: "border-[#f6dd8d] bg-[#fff8db] text-[#9a6700]",
      stopPill: "border-[#f3c3ba] bg-[#fff3f0] text-[#b42318]",
      restartPill: "border-[#cfe0fa] bg-[#edf4ff] text-[#1d4e96]",
      code: "border-slate-200 bg-[#f6f8fc] text-slate-900",
      glowA: "bg-[#3d7ddb]/16",
      glowB: "bg-[#1c4f95]/12",
      glowC: "bg-[#73a7ef]/12",
      logout: "border-[#f3c3ba] bg-[#fff3f0] text-[#b42318] hover:bg-[#ffe8e2]",
    };
  }, [themeMode]);

  const toolCards = useMemo(
    () => [
      {
        id: "chat",
        title: "Ask a Question",
        subtitle: "Talk to an AI helper",
        icon: MessageSquare,
      },
      {
        id: "docs",
        title: "Open Docs",
        subtitle: "Read helpful documents",
        icon: FileText,
      },
      {
        id: "wiki",
        title: "Read Wikipedia",
        subtitle: "Learn about anything",
        icon: BookOpen,
      },
      {
        id: "courses",
        title: "Take Courses",
        subtitle: "Study at your own pace",
        icon: GraduationCap,
      },
    ],
    []
  );

  const runRuntimeAction = (action) => {
    setActiveRuntimeAction(action);

    if (action === "refresh") {
      setTimeout(() => {
        setRuntimeStatus("good");
        setActiveRuntimeAction(null);
      }, 1100);
      return;
    }

    if (action === "toggle") {
      const nextStatus = runtimeStatus === "stopped" ? "good" : "stopped";
      setTimeout(() => {
        setRuntimeStatus(nextStatus);
        setActiveRuntimeAction(null);
      }, 900);
      return;
    }

    if (action === "start") {
      setTimeout(() => {
        setRuntimeStatus("good");
        setActiveRuntimeAction(null);
      }, 850);
      return;
    }

    if (action === "stop") {
      setTimeout(() => {
        setRuntimeStatus("stopped");
        setActiveRuntimeAction(null);
      }, 850);
      return;
    }

    if (action === "restart") {
      setRuntimeStatus("restarting");
      setTimeout(() => {
        setRuntimeStatus("good");
        setActiveRuntimeAction(null);
      }, 1800);
      return;
    }

    if (action === "auto") {
      setRuntimeStatus("okay");
      setTimeout(() => {
        setRuntimeStatus("good");
        setActiveRuntimeAction(null);
      }, 1000);
    }
  };

  return (
    <div className={`min-h-screen ${theme.page}`}>
      <div className="relative overflow-hidden">
        <div className={`pointer-events-none absolute left-[-120px] top-10 h-72 w-72 rounded-full blur-3xl ${theme.glowA}`} />
        <div className={`pointer-events-none absolute right-[-80px] top-24 h-80 w-80 rounded-full blur-3xl ${theme.glowB}`} />
        <div className={`pointer-events-none absolute bottom-[-80px] left-1/2 h-72 w-72 -translate-x-1/2 rounded-full blur-3xl ${theme.glowC}`} />

        <div className="mx-auto max-w-7xl p-5 md:p-8">
          <div className={`rounded-[30px] border ${theme.shell}`}>
            <TopBar
              theme={theme}
              themeMode={themeMode}
              setThemeMode={setThemeMode}
              viewMode={viewMode}
              setViewMode={setViewMode}
            />

            <div className="p-5 md:p-8">
              {viewMode === "portal" ? (
                <PortalHome theme={theme} toolCards={toolCards} />
              ) : (
                <AdminConsole
                  theme={theme}
                  runtimeStatus={runtimeStatus}
                  activeRuntimeAction={activeRuntimeAction}
                  runRuntimeAction={runRuntimeAction}
                  expandedPanel={expandedPanel}
                  setExpandedPanel={setExpandedPanel}
                  accountRows={accountRows}
                  setAccountRows={setAccountRows}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function TopBar({ theme, themeMode, setThemeMode, viewMode, setViewMode }) {
  return (
    <div className="flex flex-col gap-4 border-b border-inherit p-5 md:flex-row md:items-center md:justify-between md:p-6">
      <div className="flex items-center gap-4">
        <img src={logoSrc} alt="Project Puente AI logo" className="h-12 w-12 rounded-full object-cover" />
        <div>
          <div className={`text-2xl font-bold tracking-tight ${theme.text}`}>Project Puente AI</div>
          <div className={`text-sm ${theme.textSoft}`}>{viewMode === "portal" ? "Learning portal" : "Admin console"}</div>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => setViewMode(viewMode === "portal" ? "admin" : "portal")}
          className={`rounded-2xl border px-5 py-3 text-base font-semibold transition ${theme.neutralBtn}`}
        >
          <span className="inline-flex items-center gap-2">
            <Shield className="h-4 w-4" />
            {viewMode === "portal" ? "Admin" : "Portal"}
          </span>
        </button>

        <button
          type="button"
          onClick={() => setThemeMode(themeMode === "light" ? "dark" : "light")}
          className={`rounded-2xl border px-4 py-3 transition ${theme.neutralBtn}`}
          aria-label="Toggle theme"
        >
          {themeMode === "light" ? <Moon className="h-5 w-5" /> : <Sun className="h-5 w-5" />}
        </button>

        <button type="button" className={`rounded-2xl border px-5 py-3 text-base font-semibold transition ${theme.logout}`}>
          <span className="inline-flex items-center gap-2">
            <LogOut className="h-4 w-4" />
            Log Out
          </span>
        </button>
      </div>
    </div>
  );
}

function PortalHome({ theme, toolCards }) {
  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-8 text-center md:mb-10">
        <div className={`text-sm font-medium uppercase tracking-[0.22em] ${theme.textMuted}`}>Main Portal</div>
        <h1 className={`mt-3 text-4xl font-bold tracking-tight md:text-5xl ${theme.text}`}>Choose a tool</h1>
        <p className={`mx-auto mt-4 max-w-2xl text-lg leading-8 ${theme.textSoft}`}>
          Start with one of the four main learning tools below.
        </p>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {toolCards.map((tool) => (
          <PrimaryToolCard key={tool.id} theme={theme} {...tool} />
        ))}
      </div>
    </div>
  );
}

function PrimaryToolCard({ theme, title, subtitle, icon: Icon }) {
  return (
    <button
      type="button"
      className={`group min-h-[295px] rounded-[28px] border p-8 text-center transition hover:-translate-y-1 ${theme.panel}`}
    >
      <div className="flex h-full flex-col items-center justify-center">
        <div className={`mb-8 flex h-20 w-20 items-center justify-center rounded-3xl border ${theme.iconTile}`}>
          <Icon className="h-10 w-10" />
        </div>
        <div className={`text-4xl font-bold tracking-tight ${theme.text}`}>{title}</div>
        <div className={`mt-6 text-2xl ${theme.textSoft}`}>{subtitle}</div>
      </div>
    </button>
  );
}

function AdminConsole({
  theme,
  runtimeStatus,
  activeRuntimeAction,
  runRuntimeAction,
  expandedPanel,
  setExpandedPanel,
  accountRows,
  setAccountRows,
}) {
  return (
    <div className="grid gap-6">
      <div className={`rounded-[28px] border p-6 md:p-7 ${theme.panel}`}>
        <div>
          <div className={`text-sm font-medium uppercase tracking-[0.18em] ${theme.textMuted}`}>Control Center</div>
          <h1 className={`mt-2 text-4xl font-bold tracking-tight ${theme.text}`}>Admin Console</h1>
          <p className={`mt-3 text-lg ${theme.textSoft}`}>
            Manage runtime, accounts, storage insights, and security events.
          </p>
        </div>
      </div>

      <AdminRuntimeCard
        theme={theme}
        runtimeStatus={runtimeStatus}
        activeRuntimeAction={activeRuntimeAction}
        runRuntimeAction={runRuntimeAction}
      />

      <AccountsCard theme={theme} rows={accountRows} setRows={setAccountRows} />

      <div className="grid gap-6 lg:grid-cols-2">
        <ExpandablePanelCard
          theme={theme}
          title="Storage Insights"
          subtitle="Important information in a readable system log style."
          badge="Insights"
          icon={BarChart3}
          expanded={expandedPanel === "storage"}
          onToggle={() => setExpandedPanel(expandedPanel === "storage" ? null : "storage")}
        >
          <MonospaceLog theme={theme} lines={expandedPanel === "storage" ? STORAGE_LINES.compact.concat(STORAGE_LINES.expanded) : STORAGE_LINES.compact} />
        </ExpandablePanelCard>

        <ExpandablePanelCard
          theme={theme}
          title="Security / Abuse Events"
          subtitle="Recent moderation and account actions."
          badge="3 recent"
          icon={AlertTriangle}
          expanded={expandedPanel === "security"}
          onToggle={() => setExpandedPanel(expandedPanel === "security" ? null : "security")}
        >
          <MonospaceLog theme={theme} lines={expandedPanel === "security" ? SECURITY_LINES.compact.concat(SECURITY_LINES.expanded) : SECURITY_LINES.compact} />
        </ExpandablePanelCard>
      </div>
    </div>
  );
}

function AdminRuntimeCard({ theme, runtimeStatus, activeRuntimeAction, runRuntimeAction }) {
  const runtimeMeta = useMemo(() => {
    if (runtimeStatus === "okay") {
      return { label: "Okay", health: "warning", mode: "auto", llama: "running", reason: "monitoring", pill: theme.warningPill };
    }
    if (runtimeStatus === "stopped") {
      return { label: "Stopped", health: "false", mode: "auto", llama: "stopped", reason: "manual stop", pill: theme.stopPill };
    }
    if (runtimeStatus === "restarting") {
      return { label: "Restarting", health: "restarting", mode: "auto", llama: "restarting", reason: "restarting", pill: theme.restartPill };
    }
    return { label: "Running", health: "true", mode: "auto", llama: "running", reason: "ok", pill: theme.successPill };
  }, [runtimeStatus, theme]);

  return (
    <div className={`rounded-[24px] border p-5 ${theme.panelAlt}`}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className={`text-2xl font-bold ${theme.text}`}>AI Runtime</div>
          <div className={`mt-2 text-base ${theme.textSoft}`}>Clear status and controls instead of raw text blocks.</div>
        </div>
        <RuntimeStatusPill theme={theme} runtimeStatus={runtimeStatus} label={runtimeMeta.label} pillClass={runtimeMeta.pill} />
      </div>

      <div className="mt-5 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <MiniStat theme={theme} label="Health" value={runtimeMeta.health} />
        <MiniStat theme={theme} label="Mode" value={runtimeMeta.mode} />
        <MiniStat theme={theme} label="Llama" value={runtimeMeta.llama} />
        <MiniStat theme={theme} label="Reason" value={runtimeMeta.reason} />
      </div>

      <div className="mt-5 flex flex-wrap gap-3">
        <RuntimeActionButton
          theme={theme}
          label="Refresh"
          action="refresh"
          icon={RefreshCw}
          activeRuntimeAction={activeRuntimeAction}
          onClick={() => runRuntimeAction("refresh")}
          variant="blue"
        />
        <RuntimeActionButton
          theme={theme}
          label="Toggle On/Off"
          action="toggle"
          icon={Power}
          activeRuntimeAction={activeRuntimeAction}
          onClick={() => runRuntimeAction("toggle")}
          variant="blue"
        />
        <RuntimeActionButton
          theme={theme}
          label="Start"
          action="start"
          icon={Play}
          activeRuntimeAction={activeRuntimeAction}
          onClick={() => runRuntimeAction("start")}
          variant="green"
        />
        <RuntimeActionButton
          theme={theme}
          label="Stop"
          action="stop"
          icon={Square}
          activeRuntimeAction={activeRuntimeAction}
          onClick={() => runRuntimeAction("stop")}
          variant="red"
        />
        <RuntimeActionButton
          theme={theme}
          label="Restart"
          action="restart"
          icon={RotateCcw}
          activeRuntimeAction={activeRuntimeAction}
          onClick={() => runRuntimeAction("restart")}
          variant="blue"
        />
        <RuntimeActionButton
          theme={theme}
          label="Auto"
          action="auto"
          icon={Settings}
          activeRuntimeAction={activeRuntimeAction}
          onClick={() => runRuntimeAction("auto")}
          variant="blue"
        />
      </div>
    </div>
  );
}

function RuntimeStatusPill({ theme, runtimeStatus, label, pillClass }) {
  return (
    <div className={`rounded-full border px-4 py-2 text-sm font-semibold ${pillClass}`}>
      <span className="inline-flex items-center gap-2">
        <Circle className={`h-3 w-3 fill-current ${runtimeStatus === "restarting" ? "animate-pulse" : ""}`} />
        {label}
      </span>
    </div>
  );
}

function RuntimeActionButton({ theme, label, action, icon: Icon, activeRuntimeAction, onClick, variant }) {
  const isActive = activeRuntimeAction === action;

  let activeClass = theme.blueActive;
  if (variant === "green") activeClass = theme.greenActive;
  if (variant === "red") activeClass = theme.redActive;

  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-2xl border px-4 py-3 text-sm font-semibold transition ${isActive ? activeClass : theme.neutralBtn}`}
    >
      <span className="inline-flex items-center gap-2">
        <Icon className={`h-4 w-4 ${isActive && (action === "refresh" || action === "restart") ? "animate-spin" : ""}`} />
        {label}
      </span>
    </button>
  );
}

function MiniStat({ theme, label, value }) {
  return (
    <div className={`rounded-2xl border p-4 ${theme.panel}`}>
      <div className={`text-xs font-medium uppercase tracking-[0.16em] ${theme.textMuted}`}>{label}</div>
      <div className={`mt-2 text-lg font-semibold ${theme.text}`}>{value}</div>
    </div>
  );
}

function AccountsCard({ theme, rows, setRows }) {
  const [searchQuery, setSearchQuery] = useState("");
  const [openAction, setOpenAction] = useState({});
  const [pendingDelete, setPendingDelete] = useState({});
  const [actionInputs, setActionInputs] = useState({});

  const filteredRows = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) return rows;
    return rows.filter((row) => `${row.user} ${row.role}`.toLowerCase().includes(query));
  }, [rows, searchQuery]);

  const toggleAction = (user, action) => {
    setPendingDelete((current) => ({ ...current, [user]: false }));
    setOpenAction((current) => ({
      ...current,
      [user]: current[user] === action ? null : action,
    }));
  };

  const updateInputs = (user, field, value) => {
    setActionInputs((current) => ({
      ...current,
      [user]: {
        resetPassword: "",
        lockReason: "",
        lockDuration: "30 min",
        unlockReason: "",
        ...(current[user] || {}),
        [field]: value,
      },
    }));
  };

  const removeUser = (user) => {
    setRows((current) => current.filter((row) => row.user !== user));
    setPendingDelete((current) => ({ ...current, [user]: false }));
    setOpenAction((current) => ({ ...current, [user]: null }));
  };

  return (
    <div className={`rounded-[24px] border p-5 ${theme.panelAlt}`}>
      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
        <div>
          <div className={`text-2xl font-bold ${theme.text}`}>Accounts</div>
          <div className={`mt-2 text-base ${theme.textSoft}`}>Simplified actions: reset password, lock, unlock, and delete.</div>
        </div>

        <div className="relative w-full max-w-xs">
          <Search className={`pointer-events-none absolute left-4 top-1/2 h-4 w-4 -translate-y-1/2 ${theme.textMuted}`} />
          <input
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            placeholder="Search users"
            className={`w-full rounded-2xl border py-3 pl-11 pr-4 text-sm outline-none ${theme.neutralInput}`}
          />
        </div>
      </div>

      <div className="mt-5 space-y-4">
        {filteredRows.map((row) => {
          const currentAction = openAction[row.user] || null;
          const deletePending = !!pendingDelete[row.user];
          const inputs = {
            resetPassword: "",
            lockReason: "",
            lockDuration: "30 min",
            unlockReason: "",
            ...(actionInputs[row.user] || {}),
          };

          return (
            <div key={row.user} className={`rounded-3xl border p-4 ${theme.panel}`}>
              <div className="grid gap-4 xl:grid-cols-[1.1fr_0.5fr_0.7fr_0.35fr_1.2fr] xl:items-start">
                <div>
                  <div className={`text-lg font-semibold ${theme.text}`}>{row.user}</div>
                  <div className={`mt-1 text-sm ${theme.textSoft}`}>Account holder</div>
                </div>
                <div>
                  <div className={`text-sm font-medium uppercase tracking-[0.16em] ${theme.textMuted}`}>Role</div>
                  <div className={`mt-2 text-base font-semibold ${theme.text}`}>{row.role}</div>
                </div>
                <div>
                  <div className={`text-sm font-medium uppercase tracking-[0.16em] ${theme.textMuted}`}>Storage</div>
                  <div className={`mt-2 text-base font-semibold ${theme.text}`}>{row.storage}</div>
                  <div className={`mt-1 text-sm ${theme.textSoft}`}>{row.restriction}</div>
                </div>
                <div>
                  <div className={`text-sm font-medium uppercase tracking-[0.16em] ${theme.textMuted}`}>Lock</div>
                  <div className={`mt-2 text-base font-semibold ${theme.text}`}>{row.locked}</div>
                </div>

                <div>
                  <div className="flex flex-wrap gap-3">
                    <ActionToggleButton
                      theme={theme}
                      icon={KeyRound}
                      label="Reset Password"
                      danger={false}
                      onClick={() => toggleAction(row.user, "reset")}
                    />
                    <ActionToggleButton theme={theme} icon={Lock} label="Lock" onClick={() => toggleAction(row.user, "lock")} />
                    <ActionToggleButton theme={theme} icon={Unlock} label="Unlock" onClick={() => toggleAction(row.user, "unlock")} />
                    <ActionToggleButton
                      theme={theme}
                      icon={Trash2}
                      label={deletePending ? "Confirm Delete" : "Delete"}
                      danger
                      onClick={() => {
                        setOpenAction((current) => ({ ...current, [row.user]: null }));
                        setPendingDelete((current) => ({ ...current, [row.user]: !current[row.user] }));
                      }}
                    />
                  </div>

                  {currentAction === "reset" && (
                    <div className="mt-4 flex flex-wrap gap-3">
                      <input
                        value={inputs.resetPassword}
                        onChange={(event) => updateInputs(row.user, "resetPassword", event.target.value)}
                        className={`min-w-[240px] flex-1 rounded-2xl border px-4 py-3 text-sm outline-none ${theme.neutralInput}`}
                        placeholder="New password"
                      />
                      <button type="button" className={`rounded-2xl border px-4 py-3 text-sm font-semibold transition ${theme.blueActive}`}>
                        Save Password
                      </button>
                    </div>
                  )}

                  {currentAction === "lock" && (
                    <div className="mt-4 grid gap-3 md:grid-cols-[1fr_150px_auto]">
                      <input
                        value={inputs.lockReason}
                        onChange={(event) => updateInputs(row.user, "lockReason", event.target.value)}
                        className={`rounded-2xl border px-4 py-3 text-sm outline-none ${theme.neutralInput}`}
                        placeholder="Lock reason"
                      />
                      <input
                        value={inputs.lockDuration}
                        onChange={(event) => updateInputs(row.user, "lockDuration", event.target.value)}
                        className={`rounded-2xl border px-4 py-3 text-sm outline-none ${theme.neutralInput}`}
                        placeholder="30 min"
                      />
                      <button type="button" className={`rounded-2xl border px-4 py-3 text-sm font-semibold transition ${theme.neutralBtn}`}>
                        Apply Lock
                      </button>
                    </div>
                  )}

                  {currentAction === "unlock" && (
                    <div className="mt-4 flex flex-wrap gap-3">
                      <input
                        value={inputs.unlockReason}
                        onChange={(event) => updateInputs(row.user, "unlockReason", event.target.value)}
                        className={`min-w-[240px] flex-1 rounded-2xl border px-4 py-3 text-sm outline-none ${theme.neutralInput}`}
                        placeholder="Unlock reason"
                      />
                      <button type="button" className={`rounded-2xl border px-4 py-3 text-sm font-semibold transition ${theme.neutralBtn}`}>
                        Unlock Account
                      </button>
                    </div>
                  )}

                  {deletePending && (
                    <div className={`mt-4 rounded-2xl border p-4 ${theme.dangerSoft}`}>
                      <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                        <div className="text-sm font-medium">
                          This permanently deletes <span className="font-semibold">{row.user}</span>.
                        </div>
                        <div className="flex gap-3">
                          <button
                            type="button"
                            onClick={() => setPendingDelete((current) => ({ ...current, [row.user]: false }))}
                            className={`rounded-2xl border px-4 py-2 text-sm font-semibold transition ${theme.neutralBtn}`}
                          >
                            Cancel
                          </button>
                          <button
                            type="button"
                            onClick={() => removeUser(row.user)}
                            className={`rounded-2xl border px-4 py-2 text-sm font-semibold transition ${theme.redActive}`}
                          >
                            Permanently Delete
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ActionToggleButton({ theme, icon: Icon, label, danger = false, onClick }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-2xl border px-4 py-3 text-sm font-semibold transition ${danger ? theme.dangerSoft : theme.neutralBtn}`}
    >
      <span className="inline-flex items-center gap-2">
        <Icon className="h-4 w-4" />
        {label}
      </span>
    </button>
  );
}

function ExpandablePanelCard({ theme, title, subtitle, badge, icon: Icon, expanded, onToggle, children }) {
  return (
    <div className={`rounded-[24px] border p-5 transition-all duration-300 ${expanded ? "lg:col-span-2" : ""} ${theme.panelAlt}`}>
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className={`text-2xl font-bold ${theme.text}`}>{title}</div>
          <div className={`mt-2 text-base ${theme.textSoft}`}>{subtitle}</div>
        </div>
        <div className="flex items-center gap-3">
          <div className={`rounded-full border px-3 py-1.5 text-sm font-semibold ${theme.heroBadge}`}>
            <span className="inline-flex items-center gap-2">
              <Icon className="h-4 w-4" />
              {badge}
            </span>
          </div>
          <button type="button" onClick={onToggle} className={`rounded-2xl border px-4 py-3 text-sm font-semibold transition ${theme.neutralBtn}`}>
            <span className="inline-flex items-center gap-2">
              {expanded ? <Shrink className="h-4 w-4" /> : <Expand className="h-4 w-4" />}
              {expanded ? "Collapse" : "Expand"}
            </span>
          </button>
        </div>
      </div>

      <div className="mt-5">{children}</div>
    </div>
  );
}

function MonospaceLog({ theme, lines }) {
  return (
    <div className={`rounded-3xl border p-4 font-mono text-sm leading-7 ${theme.code}`}>
      {lines.map((line, index) => (
        <div key={`${index}-${line}`}>{line || <span>&nbsp;</span>}</div>
      ))}
    </div>
  );
}
