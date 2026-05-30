<?php
/**
 * fetch_trigger — Roundcube plugin for mailproxy.
 *
 * Creates a semaphore file /smph/fetch_now when the user manually
 * refreshes the mailbox. The fetcher service polls for this file and
 * starts an immediate fetch cycle when it appears.
 */
class fetch_trigger extends rcube_plugin
{
    const TRIGGER_FILE = '/smph/fetch_now';

    public function init()
    {
        $this->add_hook('refresh', [$this, 'trigger_fetch']);
    }

    public function trigger_fetch($args)
    {
        @touch(self::TRIGGER_FILE);
        return $args;
    }
}
