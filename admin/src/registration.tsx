import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import type { components } from "./api.generated";
import { APIError, read, write } from "./api";
import { Form } from "./ui";

type RegistrationSettings = components["schemas"]["RegistrationSettings"];
type RegistrationUpdate = components["schemas"]["RegistrationUpdate"];
const queryKey = ["registration-settings"];
const entries = (value: string) =>
  value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

export function RegistrationAccess() {
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) =>
      read<RegistrationSettings>("/api/registration", signal),
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  if (!query.data)
    return (
      <VStack gap={3}>
        <Text role={query.isError ? "alert" : "status"}>
          {query.isError
            ? "Could not load registration settings."
            : "Loading registration settings…"}
        </Text>
        {query.isError && (
          <Button
            label="Retry"
            variant="secondary"
            onClick={() => void query.refetch()}
          />
        )}
      </VStack>
    );
  return (
    <RegistrationForm
      settings={query.data}
      reload={async () => {
        const result = await query.refetch({ throwOnError: true });
        if (!result.data)
          throw new Error("Could not reload registration settings.");
        return result.data;
      }}
    />
  );
}

function RegistrationForm({
  settings,
  reload,
}: {
  settings: RegistrationSettings;
  reload: () => Promise<RegistrationSettings>;
}) {
  const client = useQueryClient();
  const [baseline, setBaseline] = useState(settings);
  const [enabled, setEnabled] = useState(settings.enabled ?? false);
  const [domains, setDomains] = useState(
    (settings.allowed_domains ?? []).join("\n"),
  );
  const [emails, setEmails] = useState(
    (settings.allowed_emails ?? []).join("\n"),
  );
  const availability = useQuery({
    queryKey: ["registration-availability"],
    queryFn: ({ signal }) =>
      read<components["schemas"]["RegistrationAvailability"]>(
        "/api/auth/registration",
        signal,
      ),
  });
  const apply = (saved: RegistrationSettings) => {
    setBaseline(saved);
    setEnabled(saved.enabled ?? false);
    setDomains((saved.allowed_domains ?? []).join("\n"));
    setEmails((saved.allowed_emails ?? []).join("\n"));
  };
  const save = useMutation({
    mutationFn: () => {
      const body: RegistrationUpdate = {
        expected_revision: baseline.revision,
        enabled,
        allowed_domains: entries(domains),
        allowed_emails: entries(emails),
        reason: "Registration settings updated",
      };
      return write<RegistrationSettings>("/api/registration", "PUT", body);
    },
    onSuccess: (saved) => {
      apply(saved);
      client.setQueryData(queryKey, saved);
      void client.invalidateQueries({
        queryKey: ["registration-availability"],
      });
      void client.invalidateQueries({ queryKey: ["identity-provider"] });
    },
  });
  const refresh = useMutation({
    mutationFn: reload,
    onSuccess: (saved) => {
      apply(saved);
      save.reset();
    },
  });
  const pending = save.isPending || refresh.isPending;
  const conflict =
    save.error instanceof APIError && save.error.status === 409;
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <VStack gap={4} maxWidth={640}>
        <CheckboxInput
          label="Allow self-registration"
          value={enabled}
          onChange={setEnabled}
          isDisabled={pending}
        />
        <Text color="secondary">
          Users can register when their verified email matches either list.
          New accounts are members; an administrator assigns team access.
        </Text>
        {availability.data && !availability.data.email_configured && (
          <Banner
            status="info"
            title="Email delivery is not configured"
            description="Configure email delivery in Settings to allow email/password registration. Organization sign-in can also verify allowed addresses when configured."
          />
        )}
        <TextArea
          label="Allowed email domains"
          value={domains}
          onChange={(value) => setDomains(value.slice(0, 26000))}
          description="One domain per line, up to 100. Subdomains must be listed separately."
          placeholder="sundsvall.se"
          rows={3}
          maxLength={26000}
          hasSpellCheck={false}
          isDisabled={pending}
        />
        <TextArea
          label="Allowed individual emails"
          value={emails}
          onChange={(value) => setEmails(value.slice(0, 33000))}
          description="One email per line, up to 100. Use this for people outside the allowed domains."
          placeholder="name@example.com"
          rows={3}
          maxLength={33000}
          hasSpellCheck={false}
          isDisabled={pending}
        />
        <Text type="supporting">
          Empty lists admit nobody. Closing registration or removing an
          entry does not disable existing accounts.
        </Text>
        {save.isError && (
          <Banner
            status="error"
            title="Could not save registration settings"
            description={save.error.message}
          />
        )}
        {refresh.isError && (
          <Banner
            status="error"
            title="Could not reload settings"
            description={refresh.error.message}
          />
        )}
        {save.isSuccess && (
          <Text role="status">Registration settings saved.</Text>
        )}
        <HStack gap={3} wrap="wrap">
          <Button
            label="Save registration settings"
            type="submit"
            variant="primary"
            isLoading={save.isPending}
            isDisabled={pending || conflict}
          />
          {conflict && (
            <Button
              label="Discard edits and reload"
              variant="secondary"
              isLoading={refresh.isPending}
              isDisabled={pending}
              onClick={() => refresh.mutate()}
            />
          )}
        </HStack>
      </VStack>
    </Form>
  );
}
