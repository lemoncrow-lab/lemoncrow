import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";
import { TimeRangeProvider } from "./lib/TimeRangeContext";
import { applyTheme, getInitialTheme } from "./lib/theme";

applyTheme(getInitialTheme());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <TimeRangeProvider>
        <ErrorBoundary label="App">
          <App />
        </ErrorBoundary>
      </TimeRangeProvider>
    </BrowserRouter>
  </React.StrictMode>
);
