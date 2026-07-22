import React from "react";
import ReactDOM from "react-dom/client";
import SetupApp from "./SetupApp";
import "../index.css";
import "./setup.css";

ReactDOM.createRoot(document.getElementById("setup-root")!).render(
  <React.StrictMode>
    <SetupApp />
  </React.StrictMode>,
);
