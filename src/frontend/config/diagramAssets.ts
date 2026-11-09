import { fileURLToPath } from 'node:url';

/** Shared source, independent app outputs; static-copy also serves these in development. */
export function diagramAssetTargets(): Array<{ src: string; dest: string }> {
  return ['echarts.min.js', 'mermaid.min.js'].map(name => ({
    // Windows 下 fileURLToPath 返回反斜杠路径，tinyglobby 会匹配不到，这里统一转成正斜杠
    src: fileURLToPath(new URL(`../packages/diagram-runtime/assets/${name}`, import.meta.url)).replace(/\\/g, '/'),
    dest: '.',
  }));
}
