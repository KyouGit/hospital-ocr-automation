const steps = [
  {
    title: "EMR window detection",
    operation: "Connect to target app",
    copy: "Find the Delphi TApplication window and enumerate TDrawGrid controls with pywinauto.",
    capture: 12,
    ocr: 0,
    logs: ["Load AppData/config/config.txt", "Connect: TApplication", "Find leftmost TDrawGrid"],
  },
  {
    title: "Panel capture",
    operation: "Capture Delphi grid regions",
    copy: "Use Win32 coordinates and scroll position to save left, right, and opinion regions as PNG images.",
    capture: 64,
    ocr: 8,
    logs: ["Click selected grid row", "PrintWindow full capture", "Save left/right/opinion PNG regions"],
  },
  {
    title: "Sliding-window OCR",
    operation: "Template matching by MSE",
    copy: "Move a fixed-width crop across the text image, compare each crop with character templates, then keep the lowest MSE match.",
    capture: 100,
    ocr: 72,
    logs: ["Load character templates", "Slide crop window across WBC 5.8", "Pick lowest MSE template per position"],
  },
  {
    title: "Code matching",
    operation: "Export matched CSV",
    copy: "Normalize recognized text and match it against code_list.csv to produce structured exam-code rows.",
    capture: 100,
    ocr: 100,
    logs: ["Load code_list.csv", "Normalize OCR text", "Write result_YYYYMMDD_HHMMSS.csv"],
  },
];

const matches = [
  ["CBC", "WBC", "L3011", "White blood cell count"],
  ["CBC", "RBC", "L3012", "Red blood cell count"],
  ["CBC", "Hb", "L3013", "Hemoglobin"],
  ["Inflammation", "CRP", "L5405", "C-reactive protein"],
];

let currentStep = 0;
let timer = null;

const stepTitle = document.querySelector("#step-title");
const operationTitle = document.querySelector("#operation-title");
const operationCopy = document.querySelector("#operation-copy");
const captureProgress = document.querySelector("#capture-progress");
const ocrProgress = document.querySelector("#ocr-progress");
const captureBar = document.querySelector("#capture-bar");
const ocrBar = document.querySelector("#ocr-bar");
const logList = document.querySelector("#log-list");
const resultBody = document.querySelector("#result-body");
const emrWindow = document.querySelector(".emr-window");
const buttons = [...document.querySelectorAll(".step-button")];

function renderStep(index) {
  currentStep = (index + steps.length) % steps.length;
  const step = steps[currentStep];

  stepTitle.textContent = step.title;
  operationTitle.textContent = step.operation;
  operationCopy.textContent = step.copy;
  captureProgress.textContent = `${step.capture}%`;
  ocrProgress.textContent = `${step.ocr}%`;
  captureBar.style.width = `${step.capture}%`;
  ocrBar.style.width = `${step.ocr}%`;

  logList.innerHTML = step.logs.map((log) => `<li>${log}</li>`).join("");
  buttons.forEach((button) => {
    button.classList.toggle("active", Number(button.dataset.step) === currentStep);
  });

  emrWindow.classList.toggle("ocr-focus", currentStep === 2);
  renderResults(currentStep === steps.length - 1);
}

function renderResults(done) {
  resultBody.innerHTML = matches
    .map(([panel, text, code, name]) => {
      const cells = done ? [panel, text, code, name] : [panel, text, "", ""];
      return `<tr class="${done ? "done" : ""}">${cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`;
    })
    .join("");
}

function runDemo() {
  clearInterval(timer);
  emrWindow.classList.add("running");
  renderStep(0);

  let i = 0;
  timer = setInterval(() => {
    i += 1;
    renderStep(i);
    if (i >= steps.length - 1) {
      clearInterval(timer);
      window.setTimeout(() => emrWindow.classList.remove("running"), 1400);
    }
  }, 1500);
}

document.querySelector("#run-demo").addEventListener("click", runDemo);
document.querySelector("#prev-step").addEventListener("click", () => renderStep(currentStep - 1));
document.querySelector("#next-step").addEventListener("click", () => renderStep(currentStep + 1));
buttons.forEach((button) => {
  button.addEventListener("click", () => renderStep(Number(button.dataset.step)));
});

renderStep(0);
