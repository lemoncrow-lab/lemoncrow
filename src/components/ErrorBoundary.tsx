import { Component, type ErrorInfo, type ReactNode } from "react";

type FallbackRender = (args: { error: Error; reset: () => void }) => ReactNode;

interface ErrorBoundaryProps {
  children: ReactNode;
  /** Custom fallback UI. Receives the caught error and a `reset` callback. */
  fallback?: FallbackRender;
  /** Invoked whenever the boundary catches an error (e.g. to report to Sentry). */
  onError?: (error: Error, info: ErrorInfo) => void;
  /** Invoked when the user retries. */
  onReset?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

/**
 * Catches render/lifecycle errors in its subtree, logs them, and shows a
 * fallback UI with a retry button instead of unmounting the whole app.
 *
 * Note: error boundaries do NOT catch errors in event handlers, async code,
 * SSR, or errors thrown in the boundary itself — those must be handled directly.
 */
export class ErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Log the error details for diagnostics / monitoring.
    // eslint-disable-next-line no-console
    console.error("ErrorBoundary caught an error:", error, info.componentStack);
    this.props.onError?.(error, info);
  }

  reset = (): void => {
    this.props.onReset?.();
    this.setState({ error: null });
  };

  render(): ReactNode {
    const { error } = this.state;

    if (error !== null) {
      if (this.props.fallback) {
        return this.props.fallback({ error, reset: this.reset });
      }

      return (
        <div
          role="alert"
          style={{
            padding: "1.5rem",
            border: "1px solid #f5c2c7",
            borderRadius: 8,
            background: "#fff5f5",
            color: "#842029",
            fontFamily: "system-ui, sans-serif",
          }}
        >
          <h2 style={{ margin: "0 0 0.5rem" }}>Something went wrong</h2>
          <p style={{ margin: "0 0 1rem" }}>{error.message}</p>
          <button
            type="button"
            onClick={this.reset}
            style={{
              padding: "0.5rem 1rem",
              border: "none",
              borderRadius: 6,
              background: "#842029",
              color: "#fff",
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
