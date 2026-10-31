import type { ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
import { Container, Text, matchesKey } from "@earendil-works/pi-tui";

export type MenuItem =
  | { type: "action"; id: string; label: string }
  | { type: "submenu"; id: string; label: string; children: () => MenuItem[] };

export type MenuTreeFactory = () => MenuItem[];
export type MenuActionCallback = (id: string) => Promise<void>;

interface MenuLevel {
  title: string;
  items: MenuItem[];
  selected: number;
}

export async function openMenu(
  ctx: ExtensionCommandContext,
  rootTitle: string,
  treeFactory: MenuTreeFactory,
  onAction: MenuActionCallback,
): Promise<void> {
  if (!ctx.hasUI) return;

  await ctx.ui.custom<void>((tui, theme, _kb, done) => {
    const root: MenuLevel = { title: rootTitle, items: treeFactory(), selected: 0 };
    const stack: MenuLevel[] = [root];
    let busy = false;
    const current = () => stack[stack.length - 1];

    function clampSelected(level: MenuLevel): void {
      const n = level.items.length;
      level.selected = n === 0 ? 0 : Math.max(0, Math.min(n - 1, level.selected));
    }

    function render(): string[] {
      const lines: string[] = [];
      const level = current();
      lines.push(theme.fg("accent", theme.bold(level.title)));
      lines.push("");
      for (let i = 0; i < level.items.length; i++) {
        const item = level.items[i];
        const label = item.type === "submenu" ? `${item.label} …` : item.label;
        lines.push(i === level.selected ? theme.fg("accent", `▸ ${label}`) : `  ${label}`);
      }
      lines.push("");
      lines.push(theme.fg("dim", `${stack.length > 1 ? "Esc back" : "Esc close"}  ·  Enter select`));
      return lines;
    }

    const container = new Container();
    const textComp = new Text("", 0, 0);
    container.addChild(textComp);

    function refresh(): void {
      textComp.setText(render().join("\n"));
      tui.requestRender();
    }

    function rebuildTree(): void {
      root.items = treeFactory();
      clampSelected(root);
      for (let i = 1; i < stack.length; i++) {
        const parent = stack[i - 1];
        const parentItem = parent.items[parent.selected];
        if (parentItem?.type === "submenu") {
          stack[i].items = parentItem.children();
          clampSelected(stack[i]);
        }
      }
    }

    async function executeAction(id: string): Promise<void> {
      if (busy) return;
      busy = true;
      textComp.setText(theme.fg("dim", "  …"));
      tui.requestRender();
      try {
        await onAction(id);
      } catch (error) {
        textComp.setText(theme.fg("error", `Action failed: ${error instanceof Error ? error.message : String(error)}`));
        tui.requestRender();
      } finally {
        busy = false;
        rebuildTree();
        refresh();
      }
    }

    refresh();

    return {
      render: (width: number) => container.render(width),
      invalidate: () => container.invalidate(),
      handleInput: (data: string) => {
        if (busy) return;
        const level = current();
        const n = level.items.length;
        if (matchesKey(data, "up")) {
          if (n > 0) level.selected = (level.selected - 1 + n) % n;
          refresh();
          return;
        }
        if (matchesKey(data, "down")) {
          if (n > 0) level.selected = (level.selected + 1) % n;
          refresh();
          return;
        }
        if (matchesKey(data, "escape")) {
          if (stack.length > 1) {
            stack.pop();
            clampSelected(current());
            refresh();
          } else {
            done();
          }
          return;
        }
        if (matchesKey(data, "enter")) {
          const item = level.items[level.selected];
          if (!item) return;
          if (item.type === "action") {
            void executeAction(item.id);
            return;
          }
          stack.push({ title: `${level.title} › ${item.label}`, items: item.children(), selected: 0 });
          refresh();
        }
      },
    };
  });
}
