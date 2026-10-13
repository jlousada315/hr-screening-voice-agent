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
  expect(screen.getAllByRole("radio")).toHaveLength(5);
  await user.tab();
  await user.keyboard(" ");
  expect(screen.getByRole("radio", { name: "1 star" })).toBeChecked();
  await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}");
  expect(screen.getByRole("radio", { name: "4 stars" })).toBeChecked();
  const comment = screen.getByLabelText("Tell us more (optional)");
  expect(comment).toHaveAttribute("maxlength", "2000");
  await user.type(comment, "Clearer answers");
  await user.click(screen.getByRole("button", { name: "Submit" }));
  expect(submit).toHaveBeenCalledWith(4, "Clearer answers");
});

it("allows an empty comment, announces failures and keeps skip and restart available", async () => {
  const user = userEvent.setup();
  const submit = vi.fn(), skip = vi.fn(), restart = vi.fn();
  const view = render(<CallFeedbackForm status="ready" submit={submit} skip={skip} restart={restart} />);
  await user.click(screen.getByRole("radio", { name: "5 stars" }));
  await user.click(screen.getByRole("button", { name: "Submit" }));
  expect(submit).toHaveBeenCalledWith(5, "");
  view.rerender(<CallFeedbackForm status="error" submit={submit} skip={skip} restart={restart} />);
  expect(screen.getByRole("status")).toHaveTextContent("Try again");
  await user.click(screen.getByRole("button", { name: "Skip" }));
  await user.click(screen.getByRole("button", { name: "Start another call" }));
  expect(skip).toHaveBeenCalledOnce(); expect(restart).toHaveBeenCalledOnce();
});

it("focuses the thank-you heading and provides restart after submission", async () => {
  const user = userEvent.setup();
  const restart = vi.fn(), skip = vi.fn();
  render(<CallFeedbackForm status="submitted" submit={vi.fn()} skip={skip} restart={restart} />);
  expect(screen.getByRole("heading", { name: "Thank you for your feedback" })).toHaveFocus();
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  await user.click(screen.getByRole("button", { name: "Start another call" }));
  expect(restart).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "Back to home" }));
  expect(skip).toHaveBeenCalledOnce();
});

it("previews a hovered rating without changing the chosen one, and names the value", async () => {
  const user = userEvent.setup();
  const submit = vi.fn();
  render(<CallFeedbackForm status="ready" submit={submit} skip={vi.fn()} restart={vi.fn()} />);
  await user.click(screen.getByRole("radio", { name: "4 stars" }));
  expect(screen.getByText("Good")).toBeInTheDocument();
  await user.hover(screen.getByLabelText("1 star"));
  expect(screen.getByText("Poor")).toBeInTheDocument();
  await user.unhover(screen.getByLabelText("1 star"));
  expect(screen.getByText("Good")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Submit" }));
  expect(submit).toHaveBeenCalledWith(4, "");
});
