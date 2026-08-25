import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import publicDocuments from './public-documents.json';

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
        },
        {
          to: '/docs/operations',
          label: 'Operate',
          position: 'left',
        },
        {
          to: '/docs/roadmap',
          label: 'Capabilities',
          position: 'left',
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
