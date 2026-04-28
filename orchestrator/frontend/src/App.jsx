import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Sidebar } from "./components/Sidebar.jsx";
import { MachineGrid } from "./components/MachineGrid.jsx";
import { ApplicationsPage } from "./components/ApplicationsPage.jsx";
import { SettingsPage } from "./components/SettingsPage.jsx";
import { ProfilesPage } from "./components/ProfilesPage.jsx";
import { ToastHost } from "./components/ToastHost.jsx";
import "./App.css";

const SIDEBAR_KEY = "octopilot.sidebarMinimized";

function readSidebarMinimized() {
  try {
    const v = localStorage.getItem(SIDEBAR_KEY);
    if (v === null) return false;
    return v === "1";
  } catch {
    return false;
  }
}

async function fetchJson(path, options) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.error || res.statusText || "Request failed";
    const err = new Error(msg);
    err.status = res.status;
    err.path = path;
    throw err;
  }
  return data;
}

export default function App() {
  const [sidebarMinimized, setSidebarMinimized] = useState(() => readSidebarMinimized());
  const [view, setView] = useState("machines"); // machines | applications | profiles | settings
  const [health, setHealth] = useState(null);
  const [stats, setStats] = useState(null);
  const [machines, setMachines] = useState([]);
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [soundBlocked, setSoundBlocked] = useState(false);
  const audioRef = useRef(null);

  const outOfFundsMachines = useMemo(
    () =>
      machines.filter((m) => {
        if (!m?.needs_human) return false;
        const r = String(m?.needs_human_reason || "").toLowerCase();
        return r.includes("out_of_funds") || r.includes("insufficient_quota") || r.includes("quota");
      }),
    [machines]
  );
  const outOfFundsNeeded = outOfFundsMachines.length > 0;
  const attentionNeeded = useMemo(
    () =>
      machines.some((m) => {
        if (!m?.needs_human) return false;
        // Per-machine budget exceeded uses urgent.wav inside the MachineCard overlay,
        // so suppress the global bipbop warning to avoid double audio.
        const r = String(m?.needs_human_reason || "").toLowerCase();
        if (r.includes("budget_per_machine_exceeded")) return false;
        return true;
      }),
    [machines]
  );

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_KEY, sidebarMinimized ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [sidebarMinimized]);

  const refresh = useCallback(async () => {
    try {
      const [h, s, m] = await Promise.all([
        fetchJson("/api/health"),
        fetchJson("/api/stats"),
        fetchJson("/api/machines"),
      ]);
      setHealth(h);
      setStats(s);
      setMachines(m);
      setError(null);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const refreshSettings = useCallback(async () => {
    try {
      const res = await fetchJson("/api/settings");
      if (res && res.settings) setSettings(res.settings);
    } catch {
      /* ignore – settings endpoint may not be available yet */
    }
  }, []);

  useEffect(() => {
    refreshSettings();
  }, [refreshSettings]);

  const updateSettings = useCallback(
    async (patch) => {
      try {
        const res = await fetchJson("/api/settings", {
          method: "PATCH",
          body: JSON.stringify(patch),
        });
        if (res && res.settings) setSettings(res.settings);
        setError(null);
      } catch (e) {
        setError(e.message || String(e));
        throw e;
      }
    },
    []
  );

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh, machines]);

  useEffect(() => {
    // Loop the warning sound while any machine needs human intervention.
    if (!attentionNeeded) {
      setSoundBlocked(false);
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current.currentTime = 0;
      }
      return;
    }

    if (!audioRef.current) {
      // Public asset served by Vite from /public
      audioRef.current = new Audio("/bipbop.wav");
      audioRef.current.loop = true;
      audioRef.current.preload = "auto";
    }

    const a = audioRef.current;
    const tryPlay = async () => {
      try {
        await a.play();
        setSoundBlocked(false);
      } catch {
        setSoundBlocked(true);
      }
    };

    tryPlay();

    if (!soundBlocked) return;

    const onUserGesture = () => {
      if (!attentionNeeded) return;
      tryPlay();
    };
    window.addEventListener("pointerdown", onUserGesture, { once: true });
    return () => window.removeEventListener("pointerdown", onUserGesture);
  }, [attentionNeeded, soundBlocked]);

  const ignoreOutOfFunds = useCallback(async () => {
    // Clear attention state for all affected machines (this also silences sound + hides banner).
    await Promise.allSettled(
      outOfFundsMachines.map((m) =>
        fetchJson(`/api/machines/${m.id}/attention`, { method: "POST", body: JSON.stringify({ needed: false }) })
      )
    );
    await refresh();
  }, [outOfFundsMachines, refresh]);

  const onStart = async (payload) => {
    const { url, urls, ...rest } = payload;
    const list = Array.isArray(urls) && urls.length
      ? urls.map((u) => String(u || "").trim()).filter(Boolean)
      : String(url || "")
          .trim()
          ? [String(url).trim()]
          : [];
    if (!list.length) return false;

    setError(null);
    const bodyFor = (u) => JSON.stringify({ ...rest, url: u });

    if (list.length === 1) {
      try {
        await fetchJson("/api/machines", {
          method: "POST",
          body: bodyFor(list[0]),
        });
        await refresh();
        return true;
      } catch (e) {
        setError(e.message || String(e));
        return false;
      }
    }

    const results = await Promise.allSettled(
      list.map((u) =>
        fetchJson("/api/machines", {
          method: "POST",
          body: bodyFor(u),
        })
      )
    );
    await refresh();

    const failed = results.filter((r) => r.status === "rejected");
    if (failed.length) {
      const msgs = failed.map((r) => r.reason?.message || String(r.reason));
      const uniq = [...new Set(msgs)];
      const tail = uniq.length > 2 ? "…" : "";
      const detail = uniq.slice(0, 2).join("; ") + tail;
      setError(
        failed.length === list.length
          ? detail
          : `${failed.length} of ${list.length} failed: ${detail}`
      );
      return false;
    }
    return true;
  };

  const onStop = async (id) => {
    setError(null);
    await fetchJson(`/api/machines/${id}`, { method: "DELETE" });
    await refresh();
  };

  const onRestart = async (id) => {
    setError(null);
    await fetchJson(`/api/machines/${id}/restart`, { method: "POST" });
    await refresh();
  };

  const onAgentPause = async (id, paused) => {
    setError(null);
    await fetchJson(`/api/machines/${id}/agent-pause`, {
      method: "POST",
      body: JSON.stringify({ paused }),
    });
    await refresh();
  };

  // Graceful takeover: the orchestrator writes a "stopping" control file that
  // the agent honors before its next LLM call (see agent/agent_control.py).
  // This is preferred over pause when the user wants to drive the desktop.
  const onAgentTakeover = async (id) => {
    setError(null);
    await fetchJson(`/api/machines/${id}/takeover`, { method: "POST" });
    await refresh();
  };

  const onRemove = async (id) => {
    setError(null);
    // Optimistic: remove from UI immediately (also avoids waiting for poll/refresh)
    setMachines((prev) => prev.filter((m) => m.id !== id));
    try {
      await fetchJson(`/api/machines/${id}/remove`, { method: "DELETE" });
    } catch (e) {
      // Back-compat: older backend doesn't have /remove; fallback to DELETE /api/machines/<id>
      if (e?.status === 404) {
        await fetchJson(`/api/machines/${id}`, { method: "DELETE" });
        return;
      }
      await refresh();
      throw e;
    }
  };

  return (
    <div className="app">
      <ToastHost stats={stats} settings={settings} machines={machines} />
      <Sidebar
        health={health}
        stats={stats}
        loading={loading}
        error={error}
        outOfFundsNeeded={outOfFundsNeeded}
        outOfFundsText={
          outOfFundsMachines.length === 1
            ? `LLM provider ran out of funds for machine ${outOfFundsMachines[0]?.id || ""}.`
            : `LLM provider ran out of funds for ${outOfFundsMachines.length} machine(s).`
        }
        onIgnoreOutOfFunds={ignoreOutOfFunds}
        onStart={onStart}
        onRefresh={refresh}
        minimized={sidebarMinimized}
        onToggleMinimize={() => setSidebarMinimized((m) => !m)}
        view={view}
        onChangeView={setView}
        settings={settings}
        onUpdateSettings={updateSettings}
      />
      {attentionNeeded && soundBlocked ? (
        <div className="banner warn" style={{ margin: "0.6rem 1rem 0" }}>
          <strong>Human intervention needed.</strong> Click anywhere to enable the alert sound.
        </div>
      ) : null}
      <main className="main">
        {view === "settings" ? (
          <SettingsPage />
        ) : view === "profiles" ? (
          <ProfilesPage />
        ) : view === "applications" ? (
          <ApplicationsPage />
        ) : (
          <MachineGrid
            machines={machines}
            onStop={onStop}
            onRestart={onRestart}
            onAgentPause={onAgentPause}
            onAgentTakeover={onAgentTakeover}
            onRemove={onRemove}
            settings={settings}
            onUpdateSettings={updateSettings}
          />
        )}
      </main>
    </div>
  );
}
