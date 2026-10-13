import { Typeahead } from "@astryxdesign/core/Typeahead";
import type { SearchableItem, SearchSource } from "@astryxdesign/core/Typeahead";
import { useMemo } from "react";
import type { components } from "./api.generated";

type ProviderModel = components["schemas"]["ProviderModel"];

/** Whether Hermes' model list for the account is loading, listed, or unavailable. */
export type ModelListState = "loading" | "listed" | "unavailable";

const descriptions: Record<ModelListState, string> = {
  loading: "Loading the models Hermes lists for this account.",
  listed:
    "Suggestions are the models Hermes lists for this account. You can enter another exact model name; the provider accepts or rejects it when a review runs.",
  unavailable:
    "Hermes did not list models for this account. Enter the exact model name; the provider accepts or rejects it when a review runs.",
};

function item(model: string): SearchableItem {
  return { id: model, label: model };
}

/**
 * Choose a review model by exact name, suggesting the models Hermes lists.
 *
 * Hermes' list is a picker catalog, not proof of account access: it may fall
 * back to a built-in catalog and always includes the configured model. So any
 * exact name can be chosen, the saved model always stays visible, and no other
 * model is substituted for it.
 */
export function ModelField({
  label,
  provider,
  value,
  onChange,
  listed,
  state,
  isDisabled,
}: {
  label: string;
  provider: string;
  value: string;
  onChange: (model: string) => void;
  listed: readonly ProviderModel[] | undefined;
  state: ModelListState;
  isDisabled?: boolean;
}) {
  const models = useMemo(
    () => [
      ...new Set(
        (listed ?? [])
          .filter((entry) => entry.provider === provider)
          .map((entry) => entry.model),
      ),
    ],
    [listed, provider],
  );
  const source = useMemo<SearchSource>(
    () => ({
      search: (query) => {
        const name = query.trim();
        const matches = models
          .filter((model) => model.toLowerCase().includes(name.toLowerCase()))
          .map(item);
        return name === "" || models.includes(name)
          ? matches
          : [...matches, { id: name, label: `Use “${name}”` }];
      },
      bootstrap: () => models.map(item),
    }),
    [models],
  );
  return (
    <Typeahead
      label={label}
      searchSource={source}
      value={value === "" ? null : item(value)}
      onChange={(selected) => onChange(selected?.id ?? "")}
      hasEntriesOnFocus={true}
      debounceMs={0}
      maxMenuItems={12}
      placeholder="Search or enter a model"
      emptySearchResultsText="Enter the exact model name."
      isRequired={true}
      isDisabled={isDisabled}
      description={descriptions[state]}
    />
  );
}
