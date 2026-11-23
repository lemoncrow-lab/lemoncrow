import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { WorkbenchApp as App } from "@lemoncrow/frontend";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { NativeTitleTooltipBridge } from "./components/ui/tooltip";
import "./index.css";
import { DiscoveredProductCapabilitiesProvider, LOCAL_PRODUCT_CAPABILITIES } from "./lib/RuntimeCapabilities";
import { TimeRangeProvider } from "./lib/TimeRangeContext";
import { applyTheme, getInitialTheme } from "./lib/theme";
import { forgetRetiredReviewFlow } from "./review/retiredFlowSetting";

applyTheme(getInitialTheme(), false);
forgetRetiredReviewFlow();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <DiscoveredProductCapabilitiesProvider fallback={LOCAL_PRODUCT_CAPABILITIES}>
      <TimeRangeProvider>
        <ErrorBoundary label="App">
          <NativeTitleTooltipBridge />
          <App />
        </ErrorBoundary>
      </TimeRangeProvider>
      </DiscoveredProductCapabilitiesProvider>
    </BrowserRouter>
  </React.StrictMode>
);
