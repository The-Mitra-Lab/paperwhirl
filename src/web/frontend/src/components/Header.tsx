export default function Header() {
  return (
    <header className="flex flex-col items-center pb-0 mt-[25vh]">
      <div className="flex items-center justify-center relative">
        <img
          src="/logo.png"
          alt="PaperWhirl"
          className="h-28 w-auto absolute -left-35 z-0"
        />
        <h1 className="text-5xl font-bold text-warm-800 tracking-tight relative z-10">
          PaperWhirl
        </h1>
      </div>
    </header>
  );
}
