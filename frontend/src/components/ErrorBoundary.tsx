import { Component, useEffect, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { Card, CardContent, CardHeader } from "./ui/card";
import { Button } from "./ui/button";
import { cn } from "../lib/utils";
import { isChunkLoadFailure } from "../lib/chunkLoad";

const CHUNK_RECOVERY_KEY = "lemoncrow.page.chunk-recovery";

/**
 * A missing lazy chunk after a deploy is cured by one full reload. The failing
 * message is fingerprinted in sessionStorage so a chunk that is still missing
 * after the reload shows the error card instead of reloading forever.
 */
function ChunkReloadOnce({ error }: { error: Error }) {
  useEffect(() => {
    if (!isChunkLoadFailure(error)) return;
    try {
      if (window.sessionStorage.getItem(CHUNK_RECOVERY_KEY) === error.message) return;
      window.sessionStorage.setItem(CHUNK_RECOVERY_KEY, error.message);
    } catch {
      return; // Without storage the guard cannot hold, so never auto-reload.
    }
    window.location.reload();
  }, [error]);
  return null;
}

interface ErrorBoundaryProps {
  /** Content that may throw during render. */
  children: ReactNode;
  /** Optional custom fallback. Receives the error and a retry callback. */
  fallback?: (error: Error, retry: () => void) => ReactNode;
  /** Called with the caught error and React's component stack for logging. */
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
  /** Label used in the default fallback and log messages, e.g. a page name. */
  label?: string;
  className?: string;
  /** When this value changes while an error is shown, the boundary clears it. */
  resetKey?: string;
  /** Called whenever the boundary clears an error (Retry or a resetKey change). */
  onReset?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
  errorInfo: ErrorInfo | null;
  /** Bumped on every retry so children remount with a fresh key. */
  retryCount: number;
}

/**
 * Catches render/lifecycle errors thrown by its subtree, logs them, and shows
 * a fallback UI with a retry button instead of unmounting the whole app.
 *
 * Error boundaries only catch errors during rendering, in lifecycle methods,
 * and in constructors of the tree below them — not in event handlers, async
 * code, or errors thrown in the boundary itself. See:
 * https://react.dev/reference/react/Component#catching-rendering-errors-with-an-error-boundary
 */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null, errorInfo: null, retryCount: 0 };

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    this.setState({ errorInfo });

    const label = this.props.label ?? "ErrorBoundary";
    // eslint-disable-next-line no-console -- intentional error logging sink
    console.error(
      `[${label}] caught render error:`,
      error,
      errorInfo.componentStack
    );

    this.props.onError?.(error, errorInfo);
  }

  componentDidUpdate(prevProps: ErrorBoundaryProps) {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) this.retry();
  }

  retry = () => {
    this.props.onReset?.();
    this.setState((prev) => ({
      error: null,
      errorInfo: null,
      retryCount: prev.retryCount + 1,
    }));
  };

  render() {
    const { error } = this.state;

    if (error) {
      if (this.props.fallback) {
        return this.props.fallback(error, this.retry);
      }

      const chunkFailure = isChunkLoadFailure(error);
      return (
        <>
        <ChunkReloadOnce error={error} />
        <Card tone="destructive" className={cn("m-4", this.props.className)}>
          <CardHeader className="flex flex-row items-start gap-3">
            <AlertTriangle size={18} className="mt-0.5 shrink-0 text-red-300" />
            <div className="min-w-0">
              <div className="text-sm font-semibold text-red-100">
                {this.props.label
                  ? `${this.props.label} failed to render`
                  : "Something went wrong"}
              </div>
              <div className="mt-1 text-xs leading-relaxed text-red-200/80">
                {error.message || "An unexpected error occurred."}
              </div>
              {chunkFailure && (
                <div className="mt-1 text-xs leading-relaxed text-red-200/80">
                  LemonCrow was probably updated while this tab was open. Reload the page to load the new version.
                </div>
              )}
            </div>
          </CardHeader>
          <CardContent className="flex items-center gap-3">
            <Button
              variant="destructive"
              size="sm"
              icon={<RotateCcw size={12} />}
              onClick={this.retry}
            >
              Retry
            </Button>
            {chunkFailure && (
              <Button variant="outline" size="sm" onClick={() => window.location.reload()}>
                Reload page
              </Button>
            )}
            {import.meta.env.DEV && this.state.errorInfo && (
              <details className="min-w-0 flex-1 text-caption text-red-200/60">
                <summary className="cursor-pointer select-none">
                  Stack trace
                </summary>
                <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words">
                  {error.stack}
                  {this.state.errorInfo.componentStack}
                </pre>
              </details>
            )}
          </CardContent>
        </Card>
        </>
      );
    }

    // key forces a full remount of children on retry so components with
    // broken internal state don't immediately re-throw the same error.
    return <div key={this.state.retryCount}>{this.props.children}</div>;
  }
}
