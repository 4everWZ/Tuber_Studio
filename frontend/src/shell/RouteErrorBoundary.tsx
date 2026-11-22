import { Component, type ErrorInfo, type ReactNode } from "react";
import { errorMessage } from "@/shared/lib/errors";

export interface RouteErrorBoundaryProps {
  routeKey: string;
  children: ReactNode;
}

interface RouteErrorBoundaryState {
  error: Error | null;
}

export class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Workbench route render error:", error, info.componentStack);
  }

  componentDidUpdate(prevProps: RouteErrorBoundaryProps): void {
    if (prevProps.routeKey !== this.props.routeKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <section className="panel" role="alert">
          <div className="panel-heading">
            <h2>页面渲染异常</h2>
            <button type="button" onClick={() => this.setState({ error: null })}>
              重试渲染
            </button>
          </div>
          <p className="error-text">{errorMessage(this.state.error)}</p>
        </section>
      );
    }
    return this.props.children;
  }
}
