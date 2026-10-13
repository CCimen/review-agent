import { Selector } from "@astryxdesign/core/Selector";
import type { components } from "./api.generated";

type ProviderModel = components["schemas"]["ProviderModel"];

/**
 * Choose a review model from the ones the provider account offers.
 *
 * A saved model the account no longer lists stays visible and is flagged, so
 * nothing changes silently; there is no free-text entry.
 */
export function ModelChoiceSelector({
  label,
  provider,
  value,
  onChange,
  offered,
  isLoading,
  error,
  isDisabled,
  disabledMessage,
}: {
  label: string;
  provider: string;
  value: string;
  onChange: (model: string) => void;
  offered: readonly ProviderModel[] | undefined;
  isLoading: boolean;
  error: string | null;
  isDisabled?: boolean;
  disabledMessage?: string;
}) {
  const models = (offered ?? [])
    .filter((item) => item.provider === provider)
    .map((item) => item.model);
  const missing = value !== "" && offered !== undefined && !models.includes(value);
  const options = [
    ...models.map((model) => ({ value: model, label: model })),
    ...(missing
      ? [{ value, label: `${value} (not offered by this account)` }]
      : []),
  ];
  return (
    <Selector
      label={label}
      options={options}
      value={value === "" ? undefined : value}
      onChange={onChange}
      hasSearch={options.length > 8}
      searchPlaceholder="Search models"
      placeholder="Choose a model"
      emptyText="This account offers no models for the provider. Check its login."
      isLoading={isLoading}
      isRequired={true}
      isDisabled={isDisabled || isLoading || error != null}
      disabledMessage={error ?? disabledMessage}
      description="Models the connected provider account offers."
      status={
        error != null
          ? { type: "error", message: error }
          : missing
            ? {
                type: "warning",
                message:
                  "The account no longer offers this model. Choose another before reviews fail.",
              }
            : undefined
      }
    />
  );
}
