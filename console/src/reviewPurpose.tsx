import { Selector } from "@astryxdesign/core/Selector";
import type { components } from "./api.generated";

type Purpose = components["schemas"]["ReviewPurpose"];

export function reviewPurposeLabel(purpose: Purpose) {
  return purpose === "documentation" ? "Documentation review" : "Code review";
}

export function selectedReviewPurpose(value: string | null): Purpose | "" {
  return value === "code" || value === "documentation" ? value : "";
}

export function ReviewPurposeFilter({
  value,
  onChange,
  allowAll = true,
}: {
  value: Purpose | "";
  onChange: (value: string) => void;
  allowAll?: boolean;
}) {
  return (
    <Selector
      label="Review purpose"
      value={value}
      onChange={onChange}
      options={[
        ...(allowAll ? [{ value: "", label: "All purposes" }] : []),
        { value: "code", label: "Code" },
        { value: "documentation", label: "Documentation" },
      ]}
    />
  );
}
