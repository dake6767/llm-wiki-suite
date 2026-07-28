import React from "react";
import ReactDOM from "react-dom/client";
import SetupApp from "./SetupApp";
import { initializeTheme, ThemeProvider } from "../lib/theme";
import "../index.css";
import "./setup.css";

initializeTheme();

ReactDOM.createRoot(document.getElementById("setup-root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <SetupApp />
    </ThemeProvider>
  </React.StrictMode>,
);
