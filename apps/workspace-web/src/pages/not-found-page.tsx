import { Link } from "@tanstack/react-router";
import { ArrowLeft, Map } from "lucide-react";

export function NotFoundPage() {
  return (
    <div className="not-found">
      <Map aria-hidden="true" size={34} />
      <h1>ページが見つかりません</h1>
      <p>URL を確認するか、運用ダッシュボードへ戻ってください。</p>
      <Link to="/" className="button button-primary"><ArrowLeft aria-hidden="true" size={16} />ダッシュボードへ戻る</Link>
    </div>
  );
}
