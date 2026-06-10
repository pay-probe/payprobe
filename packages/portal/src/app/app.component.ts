import { CommonModule } from "@angular/common";
import { Component } from "@angular/core";

@Component({
  selector: "app-root",
  standalone: true,
  imports: [CommonModule],
  template: `
    <div>
      <h1>PayProbe Portal</h1>
      <p>Welcome to PayProbe</p>
    </div>
  `,
  styles: [],
})
export class AppComponent {
  title = "payprobe-portal";
}
