import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import router from "./router";
import { useApp } from "./store";
import "./style.css";

const app = createApp(App);
app.use(createPinia());
app.use(router);
useApp().applyTheme();
app.mount("#app");
