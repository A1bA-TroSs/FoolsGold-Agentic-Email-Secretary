import { useT } from '../lib/i18n.js';
import { ForwardIcon, ReplyAllIcon, ReplyIcon } from './Icons.jsx';
import './ReplyBar.css';

/* The way to answer a message, pinned to the bottom of the reading pane.

   It used to be three ghost buttons inside the header -- the same weight as
   the metadata around them, and inside the block the user can resize. Drag
   the header shorter and the buttons were clipped; open a long mail and your
   eye had already moved on. People did not know the app could reply at all.

   What the research says, and what this follows:
   * visible text labels, not icons alone -- NN/g: reply and forward are not
     among the handful of icons people recognise without a word;
   * one primary action, not three equal ones -- the thing you most often do
     is answer the person who wrote to you, so that is the big target, shaped
     like the start of a reply ("Write a reply..." in Gmail, Spark, Mailspring)
     so it reads as a place to write rather than a command;
   * outside the scroll container and outside the resizable header, so it
     cannot scroll away or be covered -- the classic Outlook complaint is the
     opposite failure, reply buttons big enough to cover the sender;
   * reply-all only when there is more than one person to reply to;
   * at least 36px tall: above the 28pt macOS default control height and well
     clear of the WCAG 2.2 24px minimum.

   Bottom rather than top because the top belongs to the header, which the
   user can now make any height, and because the bottom is where the eye ends
   up after reading. */
export default function ReplyBar({ mail, onWrite }) {
  const t = useT();
  const sender = (mail.from_name || mail.from_address || '').trim();
  const everyone = [...(mail.to_recipients || []), ...(mail.cc_recipients || [])];
  const distinct = new Set(everyone.map((r) => (r.address || '').toLowerCase()).filter(Boolean));
  const canReplyAll = distinct.size > 1;

  return (
    <div className="reply-bar" role="toolbar" aria-label={t('replyBarLabel')}>
      <button type="button" className="reply-launch" data-action="reply"
              onClick={() => onWrite('reply')}>
        <ReplyIcon />
        <span className="reply-launch-text">{t('replyTo', { arg: sender })}</span>
      </button>
      {canReplyAll && (
        <button type="button" className="btn reply-alt" data-action="reply_all"
                title={t('compose_reply_all')} aria-label={t('compose_reply_all')}
                onClick={() => onWrite('reply_all')}>
          <ReplyAllIcon /><span className="reply-alt-label">{t('compose_reply_all')}</span>
        </button>
      )}
      <button type="button" className="btn reply-alt" data-action="forward"
              title={t('compose_forward')} aria-label={t('compose_forward')}
              onClick={() => onWrite('forward')}>
        <ForwardIcon /><span className="reply-alt-label">{t('compose_forward')}</span>
      </button>
    </div>
  );
}
