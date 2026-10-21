package fixture;

public class OrderService {
    private final OrderStateMachine machine = new OrderStateMachine();

    public OrderState apply(OrderState state, OrderEvent event) {
        return machine.transition(state, event);
    }
}
