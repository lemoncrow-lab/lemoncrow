import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";
import { initTelemetry } from "./lib/telemetry";
import { TimeRangeProvider } from "./lib/TimeRangeContext";
import { applyTheme, getInitialTheme } from "./lib/theme";

applyTheme(getInitialTheme());
void initTelemetry();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <TimeRangeProvider>
        <App />
      </TimeRangeProvider>
    </BrowserRouter>
  </React.StrictMode>
);
