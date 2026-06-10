import { StarLogo } from "../icons/StarLogo";

function greet(): string {
  const h = new Date().getHours();
  if (h < 5) return "Still up";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

/** 欢迎语（reference §6.7）：彩色星标 + 衬线问候，居中于内容列。 */
export function Greeting() {
  return (
    <div className="greeting">
      <StarLogo size={30} className="greeting__star" />
      <h1 className="greeting__text serif">{greet()}</h1>
    </div>
  );
}
