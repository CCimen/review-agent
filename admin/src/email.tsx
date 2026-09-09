import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import {
  SegmentedControl,
  SegmentedControlItem,
} from "@astryxdesign/core/SegmentedControl";
import { Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState, type InputHTMLAttributes } from "react";
import { APIError, read, write } from "./api";
import type { components } from "./api.generated";
import { Form } from "./ui";

type Settings = components["schemas"]["EmailSettings"];
type Configuration = components["schemas"]["SMTPConfiguration"];
const queryKey = ["email-settings"];
const empty: Configuration = {
  host: "",
  port: 587,
  sender: "",
  tls: "starttls",
  username: "",
};

export function EmailDelivery() {
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => read<Settings>("/api/email", signal),
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
  if (!query.data)
    return (
      <VStack gap={3}>
        <Text role={query.isError ? "alert" : "status"}>
          {query.isError
            ? "Could not load email settings."
            : "Loading email settings…"}
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
    <EmailForm
      settings={query.data}
      reload={async () => {
        const result = await query.refetch({ throwOnError: true });
        if (!result.data)
          throw new Error("Could not reload email settings.");
        return result.data;
      }}
    />
  );
}

function EmailForm({
  settings,
  reload,
}: {
  settings: Settings;
  reload: () => Promise<Settings>;
}) {
  const client = useQueryClient();
  const [baseline, setBaseline] = useState(settings);
  const [draft, setDraft] = useState(settings.configuration ?? empty);
  const [enabled, setEnabled] = useState(settings.enabled);
  const [password, setPassword] = useState("");
  const [clearPassword, setClearPassword] = useState(false);
  const apply = (saved: Settings) => {
    setBaseline(saved);
    setDraft(saved.configuration ?? empty);
    setEnabled(saved.enabled);
    setPassword("");
    setClearPassword(false);
  };
  const test = useMutation({
    mutationFn: () =>
      write<void>("/api/email/test", "POST", {
        expected_revision: baseline.revision,
      }),
  });
  const save = useMutation({
    mutationFn: () =>
      write<Settings>("/api/email", "PUT", {
        expected_revision: baseline.revision,
        enabled,
        configuration: draft,
        password: clearPassword ? "" : password || null,
      } satisfies components["schemas"]["EmailUpdate"]),
    onSuccess: (saved) => {
      apply(saved);
      client.setQueryData(queryKey, saved);
      test.reset();
      void client.invalidateQueries({
        queryKey: ["registration-availability"],
      });
    },
  });
  const refresh = useMutation({
    mutationFn: reload,
    onSuccess: (saved) => {
      apply(saved);
      save.reset();
      test.reset();
    },
  });
  const pending = save.isPending || test.isPending || refresh.isPending;
  const dirty =
    enabled !== baseline.enabled ||
    JSON.stringify(draft) !==
      JSON.stringify(baseline.configuration ?? empty) ||
    !!password ||
    clearPassword;
  const conflict = [save.error, test.error].some(
    (error) => error instanceof APIError && error.status === 409,
  );
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <VStack gap={5} maxWidth={760}>
        <Text color="secondary">
          Connect your SMTP provider to send registration verification
          emails. Saved changes take effect immediately.
        </Text>
        <CheckboxInput
          label="Enable email delivery"
          value={enabled}
          onChange={setEnabled}
          isDisabled={pending}
        />
        <Grid columns={{ minWidth: 240, max: 2 }} gap={4}>
          <TextInput
            label="SMTP server"
            value={draft.host}
            onChange={(host) => setDraft({ ...draft, host })}
            isRequired
            isDisabled={pending}
            placeholder="smtp.example.com"
            {...({
              required: true,
              maxLength: 253,
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />
          <NumberInput
            label="Port"
            value={draft.port}
            onChange={(port) => setDraft({ ...draft, port })}
            min={1}
            max={65535}
            isIntegerOnly
            isWheelEnabled={false}
            isRequired
            isDisabled={pending}
            {...({
              required: true,
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />
        </Grid>
        <VStack gap={2}>
          <Text>Connection security</Text>
          <SegmentedControl
            label="Connection security"
            value={draft.tls ?? "starttls"}
            isDisabled={pending}
            onChange={(value) => {
              if (value === "starttls" || value === "implicit")
                setDraft({
                  ...draft,
                  tls: value,
                  port: value === "implicit" ? 465 : 587,
                });
            }}
          >
            <SegmentedControlItem label="STARTTLS" value="starttls" />
            <SegmentedControlItem label="Implicit TLS" value="implicit" />
          </SegmentedControl>
          <Text type="supporting">
            Both options verify the server certificate. You can adjust the
            port for your provider.
          </Text>
        </VStack>
        <TextInput
          label="Sender email"
          type="email"
          value={draft.sender}
          onChange={(sender) => setDraft({ ...draft, sender })}
          isRequired
          isDisabled={pending}
          description="Use an address your SMTP provider allows you to send from."
          {...({
            required: true,
            maxLength: 320,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />
        <Grid columns={{ minWidth: 240, max: 2 }} gap={4}>
          <TextInput
            label="SMTP username"
            value={draft.username ?? ""}
            onChange={(username) => setDraft({ ...draft, username })}
            isDisabled={pending}
            isOptional
            {...({
              maxLength: 320,
              autoComplete: "off",
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
          />
          <TextInput
            label="SMTP password"
            type="password"
            value={password}
            onChange={setPassword}
            isDisabled={
              pending ||
              clearPassword ||
              !baseline.credential_storage_available
            }
            {...({
              maxLength: 4096,
              autoComplete: "new-password",
            } satisfies InputHTMLAttributes<HTMLInputElement>)}
            description={
              baseline.password_set
                ? "A password is saved. Leave blank to keep it; enter it again if you change the server or login."
                : "Set both username and password when your provider requires authentication."
            }
          />
        </Grid>
        {baseline.password_set && (
          <CheckboxInput
            label="Remove saved password"
            value={clearPassword}
            onChange={(value) => {
              setClearPassword(value);
              setPassword("");
            }}
            isDisabled={pending}
            description="Also clear the username to use an unauthenticated relay."
          />
        )}
        {!baseline.credential_storage_available && (
          <Banner
            status="info"
            title="Password storage needs a deployment key"
            description="Set REVIEW_AGENT_EMAIL_SECRET_KEY on the console service to store an encrypted SMTP password. Your deployment administrator only needs to do this once."
          />
        )}
        {save.isError && (
          <Banner
            status="error"
            title="Could not save email settings"
            description={save.error.message}
          />
        )}
        {test.isError && (
          <Banner
            status="error"
            title="Could not send test email"
            description={test.error.message}
          />
        )}
        {refresh.isError && (
          <Banner
            status="error"
            title="Could not reload email settings"
            description={refresh.error.message}
          />
        )}
        {save.isSuccess && !dirty && (
          <Text role="status">Email settings saved.</Text>
        )}
        {test.isSuccess && !dirty && (
          <Text role="status">
            Test email sent to your account email. Check your inbox.
          </Text>
        )}
        <HStack gap={3} wrap="wrap">
          <Button
            label="Save email settings"
            type="submit"
            variant="primary"
            isLoading={save.isPending}
            isDisabled={pending || conflict || !dirty}
          />
          <Button
            label="Send test to me"
            type="button"
            variant="secondary"
            isLoading={test.isPending}
            isDisabled={
              pending || conflict || dirty || !baseline.configuration
            }
            onClick={() => test.mutate()}
          />
          {conflict && (
            <Button
              label="Discard edits and reload"
              type="button"
              variant="secondary"
              isDisabled={pending}
              onClick={() => refresh.mutate()}
            />
          )}
        </HStack>
        <Text type="supporting">
          Save changes before testing. You can test delivery while it is
          disabled; test emails are limited to one per minute.
        </Text>
      </VStack>
    </Form>
  );
}
