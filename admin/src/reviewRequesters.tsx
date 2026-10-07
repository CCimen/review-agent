import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "@astryxdesign/core/Button";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { Heading, Text } from "@astryxdesign/core/Text";
import { TextInput } from "@astryxdesign/core/TextInput";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type InputHTMLAttributes } from "react";
import { read, write } from "./api";
import type { components } from "./api.generated";
import { useScope } from "./scope";
import { Form, Freshness, Loading, Saved, time } from "./ui";

type Requesters = components["schemas"]["RepositoryReviewRequesters"];

export function RepositoryReviewRequestersSection({ repositoryId, close }: {
  repositoryId: number;
  close: () => void;
}) {
  const scope = useScope();
  const cache = useQueryClient();
  const [login, setLogin] = useState("");
  const [afterUserId, setAfterUserId] = useState(0);
  const [saved, setSaved] = useState("");
  const loginInput = useRef<HTMLInputElement>(null);
  const path = scope.path(`/api/repositories/${repositoryId}/review-requesters`);
  const queryKey = ["review-requesters", repositoryId, "scoped", scope.key];
  const query = useQuery({
    queryKey: [...queryKey, afterUserId],
    queryFn: ({ signal }) => read<Requesters>(
      scope.path(`/api/repositories/${repositoryId}/review-requesters?after_user_id=${afterUserId}`), signal,
    ),
    refetchOnWindowFocus: false,
  });
  async function refresh(message: string) {
    setSaved(message);
    await cache.invalidateQueries({ queryKey });
  }
  const add = useMutation({
    mutationFn: () => write(path, "POST", {
      login: login.trim(),
    } satisfies components["schemas"]["ReviewRequesterGrant"]),
    onSuccess: async () => {
      const added = login.trim();
      setLogin("");
      setAfterUserId(0);
      await refresh(`${added} can now request reviews for this repository.`);
    },
  });
  const remove = useMutation({
    mutationFn: (userId: number) => write(
      scope.path(`/api/repositories/${repositoryId}/review-requesters/${userId}/revoke`), "POST",
    ),
    onSuccess: async () => {
      await refresh("Additional review access removed. GitHub write/admin access still applies.");
    },
  });
  useEffect(() => {
    if (remove.isSuccess) loginInput.current?.focus();
  }, [remove.isSuccess]);
  const pending = add.isPending || remove.isPending;
  const error = add.error ?? remove.error;
  return (
    <VStack as="section" gap={4}>
      <HStack gap={3} wrap="wrap" justify="between" align="center">
        <Heading level={2}>
          Who can request reviews{query.data ? ` · ${query.data.repository}` : ""}
        </Heading>
        <Button label="Close review access" variant="ghost" onClick={close} />
      </HStack>
      <Text as="p">
        Users with write or admin access to this repository on GitHub can request reviews automatically.
        Add other GitHub users below to let them use /review and /review docs without giving them write access on GitHub.
      </Text>
      <Freshness query={query} interval={false} />
      {query.isPending && <Loading rows={2} label="Loading review access" />}
      {query.data && <>
        {query.data.can_manage ? (
          <Form onSubmit={(event) => {
            event.preventDefault();
            if (pending) return;
            setSaved("");
            remove.reset();
            add.mutate();
          }}>
            <HStack gap={3} wrap="wrap" align="end">
              <TextInput
                ref={loginInput} label="GitHub username" value={login}
                onChange={setLogin} placeholder="CCimen" isDisabled={pending}
                description="Use the username of a personal GitHub account."
                width={280}
                {...({ required: true, maxLength: 39,
                  pattern: "[A-Za-z0-9][A-Za-z0-9-]{0,38}", autoComplete: "off",
                } satisfies InputHTMLAttributes<HTMLInputElement>)}
              />
              <Button type="submit" label="Allow reviews" variant="primary"
                isLoading={add.isPending} isDisabled={pending || !login.trim()} />
            </HStack>
          </Form>
        ) : <Text as="p" color="secondary">A team maintainer or platform administrator can change this list.</Text>}
        {error && <Banner status="error" title={error.message} />}
        {saved && <Saved>{saved}</Saved>}
        <List header={<Heading level={3}>Additional allowed users</Heading>} hasDividers>
          {query.data.items.map((user) => (
            <ListItem key={user.github_user_id} label={user.github_login}
              description={`GitHub account ID ${user.github_user_id} · Added ${time(user.granted_at)}`}
              endContent={query.data.can_manage ? (
                <Button label={`Remove ${user.github_login}`} variant="ghost"
                  isDisabled={pending} isLoading={remove.isPending && remove.variables === user.github_user_id}
                  onClick={() => {
                    add.reset();
                    setSaved("");
                    remove.mutate(user.github_user_id);
                  }} />
              ) : undefined}
            />
          ))}
        </List>
        {!query.data.items.length && <Text color="secondary">No additional users on this page.</Text>}
        <Text as="p" type="supporting">
          Access applies only to this repository and follows the GitHub account when its username changes.
          Removing an entry removes its additional permission; users with GitHub write or admin access remain authorized.
        </Text>
        <HStack gap={3} wrap="wrap">
          {afterUserId > 0 && <Button label="First page" onClick={() => setAfterUserId(0)} />}
          {query.data.next_after_user_id !== null && <Button label="Next users"
            onClick={() => setAfterUserId(query.data!.next_after_user_id!)} />}
        </HStack>
      </>}
    </VStack>
  );
}
