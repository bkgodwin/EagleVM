const button = document.getElementById("fullscreen-btn");
const shell = document.querySelector(".viewer-shell");

if (button && shell) {
  button.addEventListener("click", async () => {
    if (!document.fullscreenElement) {
      await shell.requestFullscreen();
      button.textContent = "Exit fullscreen";
    } else {
      await document.exitFullscreen();
      button.textContent = "Fullscreen";
    }
  });
}
