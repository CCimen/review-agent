import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import publicDocuments from './public-documents.json';

// GitHub renders `> [!TIP]` blockquotes as alerts; MDX does not. Convert the
// marker into a class so the same Markdown reads correctly on both surfaces.
const githubAlertLabels: Record<string, string> = {
  '[!NOTE]': 'note',
  '[!TIP]': 'tip',
  '[!IMPORTANT]': 'important',
  '[!WARNING]': 'warning',
  '[!CAUTION]': 'caution',
};

type MdastNode = {
  type?: string;
  value?: string;
  children?: MdastNode[];
  data?: {hProperties?: {className?: string[]}};
};

function remarkGithubAlerts() {
  const visit = (node: MdastNode): void => {
    if (node.type === 'blockquote') {
      const paragraph = node.children?.[0];
      const text = paragraph?.children?.[0];
      if (paragraph && text?.type === 'text' && typeof text.value === 'string') {
        const marker = Object.keys(githubAlertLabels).find((candidate) =>
          text.value!.startsWith(candidate),
        );
        if (marker) {
          text.value = text.value.slice(marker.length).replace(/^\s+/, '');
          if (!text.value) {
            paragraph.children!.shift();
            if (paragraph.children!.length === 0) {
              node.children!.shift();
            }
          }
          node.data = {
            ...node.data,
            hProperties: {
              className: ['gh-alert', `gh-alert--${githubAlertLabels[marker]}`],
            },
          };
        }
      }
    }
    node.children?.forEach(visit);
  };
  return (tree: MdastNode) => visit(tree);
}

const config: Config = {
  title: 'Review Agent',
  tagline: 'Evidence-backed pull-request review with deterministic controls.',
  url: 'https://ccimen.github.io',
  baseUrl: '/review-agent/',
  organizationName: 'CCimen',
  projectName: 'review-agent',
  trailingSlash: false,
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  onDuplicateRoutes: 'throw',
  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },
  markdown: {
    hooks: {
      onBrokenMarkdownLinks: 'throw',
      onBrokenMarkdownImages: 'throw',
    },
  },
  presets: [
    [
      'classic',
      {
        docs: {
          path: '..',
          include: publicDocuments,
          routeBasePath: 'docs',
          sidebarPath: './sidebars.ts',
          beforeDefaultRemarkPlugins: [remarkGithubAlerts],
          editUrl: ({docPath}) =>
            `https://github.com/CCimen/review-agent/edit/main/${docPath}`,
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],
  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      {
        indexDocs: true,
        indexBlog: false,
        indexPages: false,
        docsRouteBasePath: '/docs',
        language: 'en',
        hashed: false,
        searchBarShortcut: true,
        searchBarShortcutHint: true,
        searchBarPosition: 'right',
      },
    ],
  ],
  themeConfig: {
    colorMode: {
      defaultMode: 'light',
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Review Agent',
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'documentation',
          position: 'left',
          label: 'Docs',
        },
        {
          to: '/docs/deployment',
          label: 'Deploy',
          position: 'left',
          activeBaseRegex: '(?!)',
        },
        {
          to: '/docs/operations',
          label: 'Operate',
          position: 'left',
          activeBaseRegex: '(?!)',
        },
        {
          to: '/docs/roadmap',
          label: 'Capabilities',
          position: 'left',
          activeBaseRegex: '(?!)',
        },
        {
          href: 'https://github.com/CCimen/review-agent',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    docs: {
      sidebar: {
        hideable: true,
        autoCollapseCategories: true,
      },
    },
    footer: {
      style: 'light',
      links: [
        {
          title: 'Start here',
          items: [
            {label: 'Getting started', to: '/docs/getting-started'},
            {label: 'How reviews work', to: '/docs/how-reviews-work'},
          ],
        },
        {
          title: 'Set up',
          items: [
            {label: 'Deploy', to: '/docs/deployment'},
            {label: 'GitHub App setup', to: '/docs/github-app-pilot'},
            {label: 'Set up with an agent', to: '/docs/ai-assisted-setup'},
            {label: 'Configure behavior', to: '/docs/behavior-ownership'},
          ],
        },
        {
          title: 'Operate',
          items: [
            {label: 'Operations', to: '/docs/operations'},
            {label: 'Security model', to: '/docs/security'},
          ],
        },
        {
          title: 'Project',
          items: [
            {
              label: 'Source on GitHub',
              href: 'https://github.com/CCimen/review-agent',
            },
            {label: 'Capabilities', to: '/docs/roadmap'},
            {label: 'FAQ', to: '/docs/faq'},
          ],
        },
      ],
      copyright:
        'Documentation for Review Agent. Runtime credentials never reach this site.',
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'json', 'yaml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
