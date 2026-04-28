import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import "./index.css";

// Migration: API keys used to live in browser localStorage. They now live
// in MongoDB on the orchestrator. Wipe the stale entry so old browsers don't
// keep a copy of the user's secrets sitting around.
try {
  localStorage.removeItem("octopilot.llmKeys");
} catch {
  /* ignore */
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
