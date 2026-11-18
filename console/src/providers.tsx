import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Heading, Text } from "@astryxdesign/core/Text";
import { ScopedLink as Link } from "./scope";

export function Providers() {
  return (
    <VStack gap={4} as="section">
      <HStack gap={3} wrap="wrap" vAlign="center" hAlign="between">
        <VStack gap={3}>
          <Heading level={2}>Model connections</Heading>
          <Text as="p">
            Manage shared and team-owned provider accounts, sign-ins, and
            allowed models.
          </Text>
        </VStack>
      </HStack>
      <Link to="/model-connections">Open model connections</Link>
    </VStack>
  );
}
