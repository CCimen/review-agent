import { memo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeRaw from "rehype-raw";
import rehypeSanitize from "rehype-sanitize";

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
    <div className="review-markdown">
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
          h1: ({ children }) => <h2>{children}</h2>,
          a: ({ href, children }) => (
            <a
              href={href}
              target={href?.startsWith("#") ? undefined : "_blank"}
              rel="noreferrer"
            >
              {children}
              {href && !href.startsWith("#") ? (
                <span className="sr-only"> (opens in a new tab)</span>
              ) : null}
            </a>
          ),
          table: ({ children }) => (
            <div
              className="review-table"
              tabIndex={0}
              role="region"
              aria-label="Review table"
            >
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {markdown}
      </Markdown>
    </div>
  );
});
