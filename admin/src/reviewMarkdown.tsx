import { Code, CodeBlock } from "@astryxdesign/core/CodeBlock";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { Divider } from "@astryxdesign/core/Divider";
import { VStack } from "@astryxdesign/core/Layout";
import { Link } from "@astryxdesign/core/Link";
import { List, ListItem } from "@astryxdesign/core/List";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
} from "@astryxdesign/core/Table";
import { Heading, Text } from "@astryxdesign/core/Text";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import type { ReactNode } from "react";
import { Children, isValidElement, memo } from "react";
import Markdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";

// Published Markdown includes native details/summary elements. Parse those,
// then sanitize the resulting tree before React sees any untrusted HTML.
const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeRaw, rehypeSanitize];
const disallowedElements = ["img", "input"];

export const ReviewMarkdown = memo(function ReviewMarkdown({
  markdown,
  repository,
  headSha,
}: {
  markdown: string;
  repository: string;
  headSha: string;
}) {
  return (
    <VStack gap={4}>
      <Markdown
        remarkPlugins={remarkPlugins}
        rehypePlugins={rehypePlugins}
        disallowedElements={disallowedElements}
        urlTransform={(value) => {
          if (!value) return "";
          if (value.startsWith("#")) return value;
          try {
            const url = new URL(
              value,
              `https://github.com/${repository}/blob/${headSha}/`,
            );
            return ["https:", "http:"].includes(url.protocol) ? url.href : "";
          } catch {
            return "";
          }
        }}
        components={{
          h1: ({ children }) => <Heading level={2}>{children}</Heading>,
          h2: ({ children }) => <Heading level={2}>{children}</Heading>,
          h3: ({ children }) => <Heading level={3}>{children}</Heading>,
          h4: ({ children }) => <Heading level={4}>{children}</Heading>,
          h5: ({ children }) => <Heading level={5}>{children}</Heading>,
          h6: ({ children }) => <Heading level={6}>{children}</Heading>,
          p: ({ children }) => <Text as="p">{children}</Text>,
          ul: ({ children }) => <List listStyle="disc">{children}</List>,
          ol: ({ children, start }) => (
            <List listStyle="decimal" start={start}>
              {children}
            </List>
          ),
          li: ({ children }) => <ListItem label={children} />,
          blockquote: ({ children }) => (
            <VStack as="blockquote" gap={2} paddingInline={4}>
              {children}
            </VStack>
          ),
          hr: () => <Divider />,
          code: ({ children }) => <Code>{children}</Code>,
          pre: ({ children }) => {
            const child = Children.toArray(children)[0];
            const props = isValidElement<{
              children?: ReactNode;
              className?: string;
            }>(child)
              ? child.props
              : undefined;
            return (
              <CodeBlock
                code={String(props?.children ?? "")}
                language={props?.className?.replace(/^language-/, "")}
                width="100%"
              />
            );
          },
          details: ({ children, open }) => {
            const parts = Children.toArray(children);
            const summary = parts.find(
              (part) => isValidElement(part) && part.type === "summary",
            );
            const trigger = isValidElement<{ children?: ReactNode }>(summary)
              ? summary.props.children
              : "Details";
            return (
              <Collapsible trigger={trigger} defaultIsOpen={Boolean(open)}>
                <VStack gap={3}>
                  {parts.filter((part) => part !== summary)}
                </VStack>
              </Collapsible>
            );
          },
          a: ({ href, children }) =>
            href ? (
              <Link
                href={href}
                target={href?.startsWith("#") ? undefined : "_blank"}
                rel="noreferrer"
              >
                {children}
                {!href.startsWith("#") && (
                  <VisuallyHidden> (opens in a new tab)</VisuallyHidden>
                )}
              </Link>
            ) : (
              <Text>{children}</Text>
            ),
          table: ({ children }) => (
            <Table aria-label="Review table" textOverflow="wrap">
              {children}
            </Table>
          ),
          thead: ({ children }) => <TableHeader>{children}</TableHeader>,
          tbody: ({ children }) => <TableBody>{children}</TableBody>,
          tr: ({ children }) => <TableRow>{children}</TableRow>,
          th: ({ children }) => <TableHeaderCell>{children}</TableHeaderCell>,
          td: ({ children }) => <TableCell>{children}</TableCell>,
        }}
      >
        {markdown}
      </Markdown>
    </VStack>
  );
});
