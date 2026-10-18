import { render, screen } from "@testing-library/react";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("maps completed and running to their own non-failed styles", () => {
    render(
      <>
        <StatusBadge status="completed" />
        <StatusBadge status="running" />
      </>
    );
    expect(screen.getByText("completed")).toHaveClass("text-emerald-300");
    expect(screen.getByText("running")).toHaveClass("text-sky-300");
  });

  it("falls back to a neutral style for an unrecognized status, not the failed style", () => {
    render(<StatusBadge status="queued" />);
    const badge = screen.getByText("queued");
    expect(badge).toHaveClass("text-neutral-400");
    expect(badge).not.toHaveClass("text-red-300");
  });

  it("still styles failed and error statuses red", () => {
    render(
      <>
        <StatusBadge status="failed" />
        <StatusBadge status="error" />
      </>
    );
    expect(screen.getByText("failed")).toHaveClass("text-red-300");
    expect(screen.getByText("error")).toHaveClass("text-red-300");
  });
});
