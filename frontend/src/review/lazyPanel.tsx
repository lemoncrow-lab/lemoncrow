import { Component, lazy, useState, type ComponentType, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

import { Button as UiButton } from "../components/ui/button";

interface PanelBoundaryProps {
  label: string;
  onFailed(): void;
  onRetry(): void;
  onDismiss?(): void;
  children: ReactNode;
}

/** Contains one panel's failure so the Reader around it keeps its state. */
class PanelBoundary extends Component<PanelBoundaryProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console -- intentional error logging sink
    console.error(`[${this.props.label}] failed to open:`, error, info.componentStack);
    this.props.onFailed();
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div
        role="alert"
        className="fixed bottom-4 right-4 z-50 w-[min(360px,calc(100vw-32px))] rounded-lg border border-rose-900/60 bg-background p-3 text-caption shadow-2xl shadow-black/30"
      >
        <div className="flex items-start gap-2">
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-rose-400" aria-hidden="true" />
          <div className="min-w-0 flex-1">
            <div className="font-semibold text-foreground">{this.props.label} couldn't open</div>
            <p className="mt-1 leading-5 text-muted-foreground">
              This part of the Reader failed to load. The rest of the Review is unaffected.
            </p>
          </div>
        </div>
        <div className="mt-2 flex justify-end gap-2">
          {this.props.onDismiss && (
            <UiButton variant="ghost" size="compact" type="button" onClick={this.props.onDismiss}>
              Close
            </UiButton>
          )}
          <UiButton size="compact" type="button" onClick={this.props.onRetry}>
            Retry
          </UiButton>
        </div>
      </div>
    );
  }
}

/**
 * `React.lazy` for a panel the Reader opens on demand. A chunk that fails to
 * load (a dropped connection, a redeploy that removed it) fails only that
 * panel, with Retry, instead of unmounting the whole Reader. `React.lazy`
 * remembers a rejected import forever, so a failure swaps in a fresh one and
 * the next attempt (Retry, or opening the panel again) really loads again.
 */
export function lazyPanel<P extends object>(
  label: string,
  load: () => Promise<{ default: ComponentType<P> }>,
): ComponentType<P> {
  let Loaded = lazy(load);
  function LazyPanel(props: P) {
    const [attempt, setAttempt] = useState(0);
    const Current = Loaded as unknown as ComponentType<P>;
    return (
      <PanelBoundary
        key={attempt}
        label={label}
        onFailed={() => { Loaded = lazy(load); }}
        onRetry={() => setAttempt((count) => count + 1)}
        onDismiss={(props as { onClose?: () => void }).onClose}
      >
        <Current {...props} />
      </PanelBoundary>
    );
  }
  LazyPanel.displayName = `LazyPanel(${label})`;
  return LazyPanel;
}
