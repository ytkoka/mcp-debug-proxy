"""T2: Broker pub/sub -- non-blocking publish, drop-oldest backpressure,
near-no-op with zero subscribers, and independent delivery to multiple
subscribers.
"""
from broker import Broker


async def test_publish_with_no_subscribers_is_a_safe_no_op():
    b = Broker()
    b.publish({"x": 1})  # must not raise
    assert b.dropped_total == 0


async def test_subscribe_then_publish_delivers_record():
    b = Broker()
    queue, history = b.subscribe()
    assert history == []
    b.publish({"x": 1})
    rec = queue.get_nowait()
    assert rec == {"x": 1}


async def test_full_queue_drops_oldest_not_newest():
    b = Broker(queue_maxsize=2)
    queue, _ = b.subscribe()
    b.publish({"n": 1})
    b.publish({"n": 2})
    b.publish({"n": 3})  # queue full at this point -> drop oldest (n=1)

    assert b.dropped_total == 1
    remaining = [queue.get_nowait(), queue.get_nowait()]
    assert remaining == [{"n": 2}, {"n": 3}]
    assert queue.empty()


async def test_unsubscribe_stops_delivery():
    b = Broker()
    queue, _ = b.subscribe()
    b.unsubscribe(queue)
    b.publish({"x": 1})  # must not raise even though queue is gone
    assert queue.empty()


async def test_double_unsubscribe_is_tolerated():
    b = Broker()
    queue, _ = b.subscribe()
    b.unsubscribe(queue)
    b.unsubscribe(queue)  # must not raise


async def test_two_subscribers_each_get_the_same_record():
    b = Broker()
    q1, _ = b.subscribe()
    q2, _ = b.subscribe()
    b.publish({"x": 1})
    assert q1.get_nowait() == {"x": 1}
    assert q2.get_nowait() == {"x": 1}


async def test_subscriber_count_reflects_subscribe_and_unsubscribe():
    b = Broker()
    assert b.subscriber_count == 0
    q1, _ = b.subscribe()
    assert b.subscriber_count == 1
    q2, _ = b.subscribe()
    assert b.subscriber_count == 2
    b.unsubscribe(q1)
    assert b.subscriber_count == 1
    b.unsubscribe(q2)
    assert b.subscriber_count == 0
