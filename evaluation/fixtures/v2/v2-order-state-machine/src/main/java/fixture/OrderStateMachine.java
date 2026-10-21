package fixture;

public class OrderStateMachine {
    public OrderState transition(OrderState state, OrderEvent event) {
        if (state == OrderState.CREATED && event == OrderEvent.PAY) {
            return OrderState.CANCELLED;
        }
        if (state == OrderState.PAID && event == OrderEvent.SHIP) {
            return OrderState.SHIPPED;
        }
        if (event == OrderEvent.CANCEL) {
            return OrderState.CANCELLED;
        }
        return state;
    }
}
