import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { CallFeedbackForm } from "./CallFeedbackForm";

afterEach(cleanup);

it("focuses the heading, requires a rating, supports keyboard radios and optional comment", async () => {
  const user = userEvent.setup();
  const submit = vi.fn();
  render(<CallFeedbackForm status="ready" submit={submit} skip={vi.fn()} restart={vi.fn()} />);
  expect(screen.getByRole("heading")).toHaveFocus();
  expect(screen.getByRole("button", { name: "Submit" })).toBeDisabled();
  await user.tab();
  await user.keyboard(" ");
  expect(screen.getByRole("radio", { name: "Yes, helpful" })).toBeChecked();
  await user.keyboard("{ArrowRight}");
  expect(screen.getByRole("radio", { name: "No, not helpful" })).toBeChecked();
  const comment = screen.getByLabelText("What could we improve? (optional)");
  expect(comment).toHaveAttribute("maxlength", "2000");
  await user.type(comment, "Clearer answers");
  await user.click(screen.getByRole("button", { name: "Submit" }));
  expect(submit).toHaveBeenCalledWith("No, not helpful", "Clearer answers");
});

it("allows an empty comment, announces failures and keeps skip and restart available", async () => {
  const user = userEvent.setup();
  const submit = vi.fn(), skip = vi.fn(), restart = vi.fn();
  const view = render(<CallFeedbackForm status="ready" submit={submit} skip={skip} restart={restart} />);
  await user.click(screen.getByRole("radio", { name: "Yes, helpful" }));
  await user.click(screen.getByRole("button", { name: "Submit" }));
  expect(submit).toHaveBeenCalledWith("Yes, helpful", "");
  view.rerender(<CallFeedbackForm status="error" submit={submit} skip={skip} restart={restart} />);
  expect(screen.getByRole("status")).toHaveTextContent("Try again");
  await user.click(screen.getByRole("button", { name: "Skip" }));
  await user.click(screen.getByRole("button", { name: "Start another call" }));
  expect(skip).toHaveBeenCalledOnce(); expect(restart).toHaveBeenCalledOnce();
});

it("focuses the thank-you heading and provides restart after submission", async () => {
  const user = userEvent.setup();
  const restart = vi.fn();
  render(<CallFeedbackForm status="submitted" submit={vi.fn()} skip={vi.fn()} restart={restart} />);
  expect(screen.getByRole("heading", { name: "Thank you for your feedback" })).toHaveFocus();
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Start another call" }));
  expect(restart).toHaveBeenCalledOnce();
});
