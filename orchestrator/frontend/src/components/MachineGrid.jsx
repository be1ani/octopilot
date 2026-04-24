import { MachineCard } from "./MachineCard.jsx";
import "./MachineGrid.css";

export function MachineGrid({
  machines,
  onStop,
  onRestart,
  onAgentPause,
  onAgentTakeover,
  onRemove,
  settings,
  onUpdateSettings,
}) {
  if (!machines.length) {
    return (
      <div className="empty-grid">
        <p>No machines yet. Paste a job URL in the sidebar and start an agent.</p>
      </div>
    );
  }

  return (
    <div className="machine-grid">
      {machines.map((m) => (
        <MachineCard
          key={m.id}
          machine={m}
          onStop={onStop}
          onRestart={onRestart}
          onAgentPause={onAgentPause}
          onAgentTakeover={onAgentTakeover}
          onRemove={onRemove}
          settings={settings}
          onUpdateSettings={onUpdateSettings}
        />
      ))}
    </div>
  );
}
