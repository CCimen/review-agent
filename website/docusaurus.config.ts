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
          label: 'Documentation',
        },
        {
          to: '/docs/behavior-ownership',
          label: 'Configure',
          position: 'left',
        },
        {
          to: '/docs/operations',
          label: 'Operate',
          position: 'left',
        },
        {
          to: '/docs/roadmap',
          label: 'Architecture',
          position: 'left',
        },
        {
          to: '/docs/faq',
          label: 'FAQ',
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
            {label: 'Deploy', to: '/docs/deployment'},
            {label: 'How reviews work', to: '/docs/how-reviews-work'},
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
            {label: 'Current and planned', to: '/docs/roadmap'},
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
