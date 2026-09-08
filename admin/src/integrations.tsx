import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { Code } from "@astryxdesign/core/CodeBlock";
import type { ISODateTimeString } from "@astryxdesign/core/DateTimeInput";
import { DateTimeInput } from "@astryxdesign/core/DateTimeInput";
import { Grid } from "@astryxdesign/core/Grid";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Link as AstryxLink } from "@astryxdesign/core/Link";
import { Selector } from "@astryxdesign/core/Selector";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { useEffect, useState } from "react";
import { SettingsTabs } from "./accounts";
import type { TeamPage } from "./api";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { useScope } from "./scope";
import { ReasonAction } from "./teams";
import { Copy, Empty, Form, Freshness, dateTimeValue, time } from "./ui";

type Integration = components["schemas"]["Integration"];
type IntegrationPage = components["schemas"]["IntegrationPage"];
type IssuedIntegration = components["schemas"]["IssuedIntegration"];
type IntegrationTeam = components["schemas"]["IntegrationTeam"];

function IntegrationEditor({
  created,
  cancel,
}: {
  created: (issued: IssuedIntegration) => void;
  cancel: () => void;
}) {
  const scope = useScope();
  const [name, setName] = useState("");
  const [deploymentWide, setDeploymentWide] = useState(false);
  const [content, setContent] = useState(false);
  const [selected, setSelected] = useState<IntegrationTeam[]>([]);
  const [search, setSearch] = useState("");
  const [draftSearch, setDraftSearch] = useState("");
  const [expires, setExpires] = useState<ISODateTimeString | "">(() =>
    dateTimeValue(new Date(Date.now() + 90 * 86_400_000)),
  );
  const [reason, setReason] = useState("");
  const teams = useQuery({
    queryKey: ["integration-team-options", search, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<TeamPage>(
        `/api/teams?limit=20&search=${encodeURIComponent(search)}`,
        signal,
      ),
    enabled: !deploymentWide,
  });
  const save = useMutation({
    mutationFn: async () => {
      const issued = await write<IssuedIntegration>(
        "/api/integrations",
        "POST",
        {
          name,
          team_ids: deploymentWide ? [] : selected.map((team) => team.id),
          deployment_wide: deploymentWide,
          read_review_content: content,
          expires_at: new Date(`${expires}Z`).toISOString(),
          reason,
        } satisfies components["schemas"]["IntegrationInput"],
      );
      // Keep the one-time credential out of the shared query/mutation cache.
      created(issued);
    },
  });
  return (
    <Form
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <Heading level={2}>New integration</Heading>
      <Grid gap={4} columns={{ minWidth: 240, max: 4, repeat: "fit" }}>
        <TextInput
          label={"Application name"}
          hasAutoFocus={true}
          isRequired={true}
          value={name}
          onChange={(value) => setName(value)}
          {...({
            required: true,
            maxLength: 80,
          } satisfies InputHTMLAttributes<HTMLInputElement>)}
        />

        <Selector
          label={"Reporting scope"}
          options={[
            { value: "teams", label: "Selected teams" },
            { value: "deployment", label: "Entire deployment" },
          ]}
          value={deploymentWide ? "deployment" : "teams"}
          onChange={(value) => setDeploymentWide(value === "deployment")}
        />
      </Grid>
      {deploymentWide ? (
        <Text as="p">
          This application can report on all repositories, including unassigned
          repositories and teams added later.
        </Text>
      ) : (
        <fieldset>
          <VStack gap={4}>
            <legend>Approved teams</legend>
            <HStack gap={3} wrap="wrap" vAlign="center">
              <TextInput
                label={"Find a team"}
                value={draftSearch}
                onChange={(value) => setDraftSearch(value)}
                {...({
                  maxLength: 80,
                } satisfies InputHTMLAttributes<HTMLInputElement>)}
              />

              <Button
                label={"Search"}
                variant="secondary"
                type="button"

                onClick={() => setSearch(draftSearch.trim())}
              />
            </HStack>
            <Freshness query={teams} quiet />
            {selected.length > 0 && (
              <HStack
                gap={3}
                wrap="wrap"
                vAlign="center"
                aria-label="Selected teams"
              >
                {selected.map((team) => (
                  <Button
                    label={"Remove " + team.name}
                    variant="secondary"
                    type="button"

                    key={team.id}
                    onClick={() =>
                      setSelected((items) =>
                        items.filter((item) => item.id !== team.id),
                      )
                    }
                  />
                ))}
              </HStack>
            )}
            <VStack gap={4}>
              {teams.data?.items.map((team) => (
                <VStack gap={2} key={team.id}>
                  <CheckboxInput
                    label={team.name}
                    value={selected.some((item) => item.id === team.id)}
                    isDisabled={
                      selected.length >= 100 &&
                      !selected.some((item) => item.id === team.id)
                    }
                    onChange={(value) =>
                      setSelected((items) =>
                        value
                          ? [...items, { id: team.id, name: team.name }]
                          : items.filter((item) => item.id !== team.id),
                      )
                    }
                  />
                </VStack>
              ))}
            </VStack>
            {teams.data?.items.length === 0 && (
              <Text as="p">No teams match this search.</Text>
            )}
            {teams.data?.next_after_id !== null &&
              teams.data?.next_after_id !== undefined && (
                <Text as="p">
                  Showing the first 20 matches. Narrow the search to find
                  another team.
                </Text>
              )}
          </VStack>
        </fieldset>
      )}

      <CheckboxInput
        label={"Allow published review content"}
        value={content}
        onChange={(value) => setContent(value)}
      />

      <Text as="p" color="secondary">
        All integrations can read outcome metadata and aggregate reports. This
        additional permission exposes published review text within the approved
        scope.
      </Text>
      <DateTimeInput
        label="Expires at (UTC)"
        isRequired
        value={expires || undefined}
        onChange={(value) => setExpires(value ?? "")}
        hourFormat="24h"
      />

      <TextArea
        label={"Reason"}
        isRequired={true}
        maxLength={500}
        value={reason}
        onChange={(value) => setReason(value.slice(0, 500))}
        {...({
          required: true,
        } satisfies TextareaHTMLAttributes<HTMLTextAreaElement>)}
      />

      {save.isError && (
        <Text as="p" role="alert">
          {save.error.message}
        </Text>
      )}
      <HStack gap={3} wrap="wrap" vAlign="center">
        <Button
          label={String(save.isPending ? "Creating…" : "Create integration")}
          variant="primary"
          type="submit"
          isDisabled={
            save.isPending ||
            !name.trim() ||
            !reason.trim() ||
            !expires ||
            (!deploymentWide && selected.length === 0)
          }
        />
        <Button
          label={"Cancel"}
          variant="secondary"
          type="button"

          isDisabled={save.isPending}
          onClick={cancel}
        />
      </HStack>
    </Form>
  );
}

function IntegrationRow({
  integration,
  refresh,
}: {
  integration: Integration;
  refresh: () => void;
}) {
  return (
    <TableRow>
      <TableHeaderCell scope="row">
        {integration.name}
        <Text color="secondary"> · #{integration.id}</Text>
      </TableHeaderCell>
      <TableCell>
        {integration.deployment_wide
          ? "Entire deployment"
          : integration.teams.map((team) => team.name).join(", ")}
      </TableCell>
      <TableCell>
        {integration.read_review_content
          ? "Reports and published content"
          : "Reports only"}
      </TableCell>
      <TableCell>{time(integration.expires_at)}</TableCell>
      <TableCell>
        {integration.state === "active"
          ? "Active"
          : integration.state === "expired"
            ? "Expired"
            : "Revoked"}
      </TableCell>
      <TableCell>
        {integration.state === "active" && (
          <ReasonAction
            label="Revoke"
            path={`/api/integrations/${integration.id}/revoke`}
            description="Subsequent reads will fail. Requests already in progress may finish."
            danger
            done={refresh}
          />
        )}
      </TableCell>
    </TableRow>
  );
}

export function IntegrationsPage() {
  const scope = useScope();
  const client = useQueryClient();
  const [afterId, setAfterId] = useState(0);
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<IssuedIntegration | null>(null);
  const query = useQuery({
    queryKey: ["integrations", afterId, "scoped", scope.key],
    queryFn: ({ signal }) =>
      read<IntegrationPage>(
        `/api/integrations?after_id=${afterId}&limit=50`,
        signal,
      ),
  });
  const nextAfterId = query.data?.next_after_id;
  function refresh() {
    void client.invalidateQueries({ queryKey: ["integrations"] });
  }
  useEffect(() => {
    document.title = "Review Agent · Integrations";
  }, []);
  return (
    <>
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={1}>Integrations</Heading>
          <Text as="p">
            Platform administrators grant applications access to approved
            reports.
          </Text>
        </VStack>
        {!creating && !issued && (
          <Button
            label={"New integration"}
            variant="primary"
            type="submit"
            onClick={() => setCreating(true)}
          />
        )}
      </HStack>
      <SettingsTabs />
      {issued && (
        <VStack
          gap={4}
          as="section"

          aria-labelledby="integration-credential-title"
        >
          <Heading level={2} id="integration-credential-title">
            Save the credential for {issued.integration.name}
          </Heading>
          <Text as="p">
            This is the only time it is shown. Store it in your application's
            secret manager and send it in the Authorization header as a Bearer
            credential.
          </Text>
          <Copy value={issued.token} label="integration credential">
            <Code>{issued.token}</Code>
          </Copy>
          <Text as="p">
            <Button
              label={"I have saved it"}
              variant="secondary"
              type="submit"
              onClick={() => setIssued(null)}
            />
          </Text>
        </VStack>
      )}
      {creating && (
        <IntegrationEditor
          cancel={() => setCreating(false)}
          created={(result) => {
            setIssued(result);
            setCreating(false);
            setAfterId(0);
            refresh();
          }}
        />
      )}
      <Freshness query={query} />
      {query.data &&
        (query.data.items.length ? (
          <VStack
            gap={4}

            tabIndex={0}
            role="region"
            aria-label="Application integrations"
          >
            <Table>
              <caption>
                <Text type="supporting" display="block" justify="start">
                  Credential expiry and granted access
                </Text>
              </caption>
              <TableHeader>
                <TableRow>
                  <TableHeaderCell scope="col">Application</TableHeaderCell>
                  <TableHeaderCell scope="col">Scope</TableHeaderCell>
                  <TableHeaderCell scope="col">Access</TableHeaderCell>
                  <TableHeaderCell scope="col">Expires</TableHeaderCell>
                  <TableHeaderCell scope="col">State</TableHeaderCell>
                  <TableHeaderCell scope="col">Action</TableHeaderCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.items.map((integration) => (
                  <IntegrationRow
                    key={integration.id}
                    integration={integration}
                    refresh={refresh}
                  />
                ))}
              </TableBody>
            </Table>
          </VStack>
        ) : (
          <Empty title="No integrations">
            Create a credential when an application needs to read team reports.
          </Empty>
        ))}
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        {afterId > 0 && (
          <Button
            label={"First page"}
            variant="secondary"
            type="submit"
            onClick={() => setAfterId(0)}
          />
        )}
        {nextAfterId != null && (
          <Button
            label={"Next page"}
            variant="secondary"
            type="submit"
            onClick={() => setAfterId(nextAfterId)}
          />
        )}
      </HStack>
      <Text as="p" color="secondary">
        Permissions are fixed when a credential is created. To change access or
        rotate a credential, create its replacement and then revoke the old
        integration.
      </Text>
      <Text as="p">
        <AstryxLink
          href="/api/docs#integration%20reports"
          target="_blank"
          rel="noreferrer"
        >
          Open the API reference
        </AstryxLink>{" "}
        for request parameters, authentication and response schemas.
      </Text>
    </>
  );
}
