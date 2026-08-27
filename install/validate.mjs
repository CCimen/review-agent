#!/usr/bin/env node

import {readFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

import Ajv2020 from 'ajv/dist/2020.js';
import {parse} from 'yaml';

const installRoot = resolve(fileURLToPath(new URL('.', import.meta.url)));
const schemaPath = resolve(installRoot, 'review-agent.schema.json');
const examplePath = resolve(installRoot, 'review-agent.example.yaml');

async function readText(path, description) {
  try {
    return await readFile(path, 'utf8');
  } catch {
    throw new Error(`Cannot read ${description}: ${path}`);
  }
}

function parseYaml(text) {
  try {
    return parse(text);
  } catch {
    throw new Error('Installation plan is not valid YAML.');
  }
}

function placeholderErrors(config, example) {
  const errors = [];
  if (config?.deployment?.public_url === example?.deployment?.public_url) {
    errors.push('/deployment/public_url: replace the example URL');
  }
  if (config?.deployment?.image?.digest === example?.deployment?.image?.digest) {
    errors.push('/deployment/image/digest: replace the example digest');
  }
  return errors;
}

function schemaErrors(validate, config) {
  if (validate(config)) {
    return [];
  }
  return (validate.errors ?? []).map((error) => {
    const location = error.instancePath || '/';
    return `${location}: ${error.message}`;
  });
}

async function main() {
  const arguments_ = process.argv.slice(2);
  const allowExample = arguments_.includes('--allow-example');
  const requestedPath = arguments_.find((argument) => !argument.startsWith('--'));
  const configPath = resolve(process.cwd(), requestedPath ?? examplePath);

  const schema = JSON.parse(await readText(schemaPath, 'installation schema'));
  const example = parseYaml(await readText(examplePath, 'example installation plan'));
  const config = parseYaml(await readText(configPath, 'installation plan'));
  const validate = new Ajv2020({allErrors: true, strict: true}).compile(schema);
  const errors = schemaErrors(validate, config);
  if (!allowExample) {
    errors.push(...placeholderErrors(config, example));
  }

  if (errors.length > 0) {
    for (const error of errors) {
      process.stderr.write(`${error}\n`);
    }
    process.exitCode = 1;
    return;
  }
  process.stdout.write(`Installation plan is valid: ${configPath}\n`);
}

try {
  await main();
} catch (error) {
  const message =
    error instanceof Error
      ? error.message
      : 'Installation plan validation failed.';
  process.stderr.write(`${message}\n`);
  process.exitCode = 1;
}
