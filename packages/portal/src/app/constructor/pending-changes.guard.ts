import { inject } from "@angular/core";
import { CanDeactivateFn } from "@angular/router";

import { UiService } from "../shared/ui.service";
import { ConstructorComponent } from "./editor/constructor.component";

/** Blocks in-app navigation away from the editor while there are unsaved changes. */
export const pendingChangesGuard: CanDeactivateFn<ConstructorComponent> = (
  component,
) => {
  if (!component.dirty()) return true;
  return inject(UiService).confirm(
    "You have unsaved changes that will be lost.\nLeave the editor anyway?",
    { title: "Unsaved changes", confirmLabel: "Discard & leave", danger: true },
  );
};
